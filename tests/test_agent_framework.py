import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literature_desk.agent.runtime import _safe_context, _tool_payload

os.environ["LITLOOM_DATABASE_URL"] = ""
os.environ["LITLOOM_TESTING"] = "1"
import app
from literature_desk.agent.tools import build_langchain_tools


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class AgentFrameworkTests(unittest.TestCase):
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

    def test_context_bounds_preserve_structured_goal_fields(self):
        context = _safe_context({
            "goal_id": "goal-fixture",
            "sources": ["arxiv", "openalex"],
            "focus": ["multimodal", {"topic": "retrieval"}],
            "time_range": {"from": 2020, "to": 2026},
            "notes": "x" * 5000,
        })
        self.assertEqual(context["sources"], ["arxiv", "openalex"])
        self.assertEqual(context["focus"][1], {"topic": "retrieval"})
        self.assertEqual(context["time_range"], {"from": 2020, "to": 2026})
        self.assertEqual(len(context["notes"]), 4000)
        payload = _tool_payload("search_sources", "research objective", context, {})
        self.assertEqual(payload["goal"]["sources"], ["arxiv", "openalex"])
        self.assertEqual(payload["goal"]["time_range"], {"from": 2020, "to": 2026})

    def test_langgraph_agent_uses_native_tools_and_replans_from_tool_results(self):
        responses = [
            {
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "search_local_library",
                                "arguments": '{"query":"多模态食物体积估计","limit":5}',
                            },
                        }]
                    }
                }]
            },
            {
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "id": "call-2",
                            "type": "function",
                            "function": {
                                "name": "compare_papers",
                                "arguments": '{"papers":[{"id":"p1","title":"Fixture paper"}],"claims":[]}',
                            },
                        }]
                    }
                }]
            },
            {"choices": [{"message": {"content": "已检查本地论文并比较证据。"}}]},
        ]
        payloads = []

        def open_request(_endpoint, payload, _key, _timeout):
            payloads.append(payload)
            return _FakeResponse()

        def execute_tool(name, _payload):
            if name == "search_local_library":
                return {"results": [{"id": "p1", "title": "Fixture paper"}], "total": 1}
            if name == "compare_papers":
                return {"rows": [{"title": "Fixture paper", "evidence": "abstract"}], "row_count": 1}
            raise AssertionError(name)

        model_settings = {
            "enabled": True,
            "protocol": "chat_completions",
            "model": "fixture-model",
            "temperature": 0.1,
            "api_url": "https://model.example/v1/chat/completions",
        }
        with (
            patch.object(app, "model_settings", return_value=model_settings),
            patch.object(app, "model_api_key", return_value="fixture-key"),
            patch.object(app, "open_compatible_model_request", side_effect=open_request),
            patch.object(app, "read_model_response", side_effect=responses),
            patch.object(app, "call_agent_tool", side_effect=execute_tool),
        ):
            result = app.autonomous_agent(
                {"objective": "检查馆藏中的食物体积估计论文并比较证据", "max_steps": 3}
            )

        self.assertEqual(result["framework"], "langgraph")
        self.assertEqual(result["status"], "success")
        self.assertEqual([item["tool"] for item in result["history"]], [
            "search_local_library", "compare_papers"
        ])
        self.assertEqual(len(payloads), 3)
        self.assertTrue(payloads[0]["tools"])
        self.assertEqual(payloads[0]["tool_choice"], "auto")
        second_turn = json.loads(payloads[1]["messages"][1]["content"])
        self.assertEqual(second_turn["tool_history"][0]["tool"], "search_local_library")
        self.assertIn("Fixture paper", json.dumps(second_turn["tool_history"], ensure_ascii=False))
        snapshot = app.agent_run_snapshot(result["run_id"])
        self.assertEqual(snapshot["status"], "success")
        self.assertEqual(len(snapshot["tool_calls"]), 2)
        self.assertTrue(any(item["artifact_type"] == "agent_autonomous_result" for item in snapshot["artifacts"]))

    def test_autonomous_research_goal_is_not_a_prebuilt_step_dag(self):
        plan = app.build_agent_plan({"workflow": "autonomous", "scope": {}})
        self.assertEqual(plan["execution_mode"], "model_selected_tools")
        self.assertEqual(plan["steps"], [])
        self.assertGreater(len(plan["available_tools"]), 0)

    def test_langchain_config_is_injected_and_hidden_from_tool_input_schema(self):
        tool = build_langchain_tools()["search_local_library"]
        self.assertEqual(set(tool.args), {"payload_json"})
        with patch.object(app, "_agent_execute_tool", return_value={"total": 0}) as execute:
            output = tool.invoke(
                {"payload_json": "{}"},
                config={
                    "metadata": {
                        "litloom_run_id": "run-fixture",
                        "litloom_step_id": "step-fixture",
                        "litloom_tool_class": "Read",
                    }
                },
            )
        self.assertEqual(json.loads(output), {"total": 0})
        execute.assert_called_once_with(
            "run-fixture", "step-fixture", "search_local_library", "Read", {}
        )

    def test_write_agent_action_waits_for_confirmation(self):
        response = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "call-confirm",
                        "type": "function",
                        "function": {
                            "name": "draft_reading_card",
                            "arguments": '{"paper":{"id":"candidate:paper-1","title":"Fixture paper"}}',
                        },
                    }]
                }
            }]
        }
        model_settings = {
            "enabled": True,
            "protocol": "chat_completions",
            "model": "fixture-model",
            "temperature": 0.1,
            "api_url": "https://model.example/v1/chat/completions",
        }
        with (
            patch.object(app, "model_settings", return_value=model_settings),
            patch.object(app, "model_api_key", return_value="fixture-key"),
            patch.object(app, "open_compatible_model_request", return_value=_FakeResponse()),
            patch.object(app, "read_model_response", return_value=response),
            patch.object(app, "call_agent_tool") as execute,
        ):
            result = app.autonomous_agent(
                {"objective": "分析候选论文并生成阅读卡", "max_steps": 2}
            )
        snapshot = app.agent_run_snapshot(result["run_id"])
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(snapshot["status"], "awaiting_confirmation")
        self.assertEqual(snapshot["steps"][0]["status"], "awaiting_confirmation")
        execute.assert_not_called()

    def test_autonomous_citation_candidates_can_be_imported_after_explicit_confirmation(self):
        candidate = {
            "id": "candidate:citation:fixture",
            "title": "Citation expansion paper",
            "doi": "10.1000/fixture",
            "source": "openalex",
        }
        snapshots = [
            {"workflow": "autonomous"},
            {"steps": [{
                "status": "success",
                "tool_name": "expand_citations",
                "output": {"papers": [candidate]},
            }]},
        ]
        with (
            patch.object(app, "agent_run_snapshot", side_effect=snapshots),
            patch.object(app, "upsert_paper", return_value=("paper-fixture", True)) as upsert,
        ):
            result = app.import_agent_candidates(
                "run-fixture", [candidate["id"]], confirm=True
            )
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["candidate_map"][candidate["id"]], "paper-fixture")
        upsert.assert_called_once()

    def test_agent_requires_configured_model_instead_of_using_a_fixed_fallback(self):
        with (
            patch.object(app, "model_settings", return_value={"enabled": False}),
            patch.object(app, "model_api_key", return_value=""),
        ):
            with self.assertRaisesRegex(ValueError, "配置模型 API"):
                app.autonomous_agent({"objective": "自主检查并梳理多模态研究论文"})

    def test_framework_status_reports_dynamic_langgraph_capability(self):
        status = app.agent_framework_status()
        self.assertEqual(status["runtime"], "langgraph")
        self.assertTrue(status["langchain"])
        self.assertTrue(status["autonomous_tool_selection"])
        self.assertTrue(status["model_native_tool_calls"])
        self.assertGreater(status["tool_count"], 0)
        self.assertEqual(status["decision_modes"], ["call_tool", "reflect", "finish"])
        self.assertTrue(status["memory"])
        self.assertEqual(status["graph_nodes"], ["decide", "execute", "reflect"])

    def test_autonomous_agent_injects_goal_memory_on_first_turn(self):
        response = {"choices": [{"message": {"content": "已基于历史记忆完成回答。"}}]}
        payloads = []

        def open_request(_endpoint, payload, _key, _timeout):
            payloads.append(payload)
            return _FakeResponse()

        goal = app.create_research_goal({
            "objective": "长期记忆测试：多模态证据",
            "core_question": "多模态证据",
            "sources": ["arxiv"],
        })
        with app.db_session() as db:
            paper_id, _ = app.upsert_paper(
                {"title": "历史多模态证据论文", "year": 2025}, source="manual"
            )
            now = app.now_iso()
            db.execute(
                "INSERT INTO research_goal_papers(goal_id,paper_key,local_paper_id,title,first_seen_at,last_seen_at,seen_count,user_decision,reading_status) VALUES(?,?,?,?,?,?,?,?,?)",
                (goal["id"], "paper:" + paper_id, paper_id, "", now, now, 1, "relevant", "saved"),
            )
        model_settings = {
            "enabled": True,
            "protocol": "chat_completions",
            "model": "fixture-model",
            "temperature": 0.1,
            "api_url": "https://model.example/v1/chat/completions",
        }
        with (
            patch.object(app, "model_settings", return_value=model_settings),
            patch.object(app, "model_api_key", return_value="fixture-key"),
            patch.object(app, "open_compatible_model_request", side_effect=open_request),
            patch.object(app, "read_model_response", return_value=response),
        ):
            result = app.autonomous_agent({
                "objective": "请基于历史证据回答多模态问题",
                "goal_id": goal["id"],
                "max_steps": 1,
            })
        self.assertEqual(result["status"], "success")
        turn = json.loads(payloads[0]["messages"][1]["content"])
        self.assertEqual(turn["durable_memory"]["papers"][0]["title"], "历史多模态证据论文")
        self.assertEqual(turn["durable_memory"]["feedback"][0]["decision"], "relevant")

    def test_reflection_is_a_real_graph_action_and_persisted(self):
        responses = [
            {"choices": [{"message": {"content": '{"action":"reflect","answer":"证据仍需核验"}'}}]},
            {"choices": [{"message": {"content": "当前证据不足，已明确缺口。"}}]},
        ]
        payloads = []

        def open_request(_endpoint, payload, _key, _timeout):
            payloads.append(payload)
            return _FakeResponse()

        model_settings = {
            "enabled": True,
            "protocol": "chat_completions",
            "model": "fixture-model",
            "temperature": 0.1,
            "api_url": "https://model.example/v1/chat/completions",
        }
        with (
            patch.object(app, "model_settings", return_value=model_settings),
            patch.object(app, "model_api_key", return_value="fixture-key"),
            patch.object(app, "open_compatible_model_request", side_effect=open_request),
            patch.object(app, "read_model_response", side_effect=responses),
        ):
            result = app.autonomous_agent({"objective": "复核研究证据并说明缺口", "max_steps": 2})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["history"][0]["tool"], "reflection")
        self.assertTrue(any(item["artifact_type"] == "agent_reflection" for item in app.agent_run_snapshot(result["run_id"])["artifacts"]))


if __name__ == "__main__":
    unittest.main()
