import io
import json
import unittest
from unittest.mock import patch

import mcp_server


class FakeClient:
    def __init__(self):
        self.base_url = "http://127.0.0.1:8772"
        self.calls = []

    def get(self, path, timeout=None):
        self.calls.append(("GET", path, None))
        if path == "/api/health":
            return {"status": "ok", "ready": True}
        if path.startswith("/api/runs/"):
            return {"id": path.split("/")[3], "status": "success", "results": [{"title": "Fixture"}]}
        if path == "/api/discovery/history?limit=10&q=":
            return {"history": []}
        return {"ok": True, "path": path}

    def post(self, path, payload=None, timeout=None):
        self.calls.append(("POST", path, payload))
        if path == "/api/discover":
            return {"id": "run-fixture"}
        if "/retry" in path:
            return {"id": path.split("/")[3], "status": "queued"}
        return {"id": "job-fixture", "status": "queued"}


class MCPAdapterTests(unittest.TestCase):
    def test_initialize_and_tools_list_are_valid_mcp_messages(self):
        initialized = mcp_server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(initialized["jsonrpc"], "2.0")
        self.assertEqual(initialized["id"], 1)
        self.assertIn("tools", initialized["result"]["capabilities"])
        listed = mcp_server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {item["name"] for item in listed["result"]["tools"]}
        self.assertIn("litloom_search_papers", names)
        self.assertIn("litloom_get_recommendations", names)
        self.assertIn("litloom_browse_web", names)
        self.assertIn("litloom_fetch_web_page", names)
        self.assertIn("litloom_start_analysis", names)
        self.assertIn("litloom_build_paper_overview", names)
        self.assertIn("litloom_build_semantic_index", names)

    def test_professional_overview_tool_uses_local_read_only_endpoint(self):
        client = FakeClient()
        result = mcp_server.call_tool("litloom_build_paper_overview", {"paper_id": "paper/1"}, client)
        self.assertEqual(result["path"], "/api/papers/paper%2F1/agent-overview")
        self.assertEqual(client.calls, [("GET", "/api/papers/paper%2F1/agent-overview", None)])

    def test_notifications_do_not_emit_a_response(self):
        self.assertIsNone(mcp_server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_search_starts_durable_job_and_can_poll_it(self):
        client = FakeClient()
        result = mcp_server.call_tool("litloom_search_papers", {"query": "food volume", "mode": "model_web", "wait_seconds": 0}, client)
        self.assertEqual(result["run_id"], "run-fixture")
        self.assertEqual(result["status"], "success")
        self.assertEqual(client.calls[0][0], "POST")
        self.assertTrue(client.calls[0][2]["options"]["parallel_sources"])
        self.assertEqual(client.calls[0][2]["sources"], [])

    def test_connector_search_has_safe_default_sources(self):
        client = FakeClient()
        mcp_server.call_tool("litloom_search_papers", {"query": "food volume", "mode": "connectors"}, client)
        self.assertEqual(client.calls[0][2]["sources"], ["arxiv", "openalex", "crossref"])

    def test_connector_search_accepts_explicit_tavily_discovery_source(self):
        client = FakeClient()
        mcp_server.call_tool(
            "litloom_search_papers",
            {"query": "food volume", "mode": "connectors", "sources": ["tavily"]},
            client,
        )
        self.assertEqual(client.calls[0][2]["sources"], ["tavily"])

    def test_connector_search_accepts_explicit_deepseek_source(self):
        client = FakeClient()
        mcp_server.call_tool(
            "litloom_search_papers",
            {"query": "food volume", "mode": "connectors", "sources": ["deepseek_search"]},
            client,
        )
        self.assertEqual(client.calls[0][2]["sources"], ["deepseek_search"])

    def test_recommendation_tool_uses_provider_aware_read_only_endpoint(self):
        client = FakeClient()
        result = mcp_server.call_tool("litloom_get_recommendations", {"limit": 6}, client)
        self.assertEqual(result["path"], "/api/recommendations?limit=6")
        self.assertEqual(client.calls, [("GET", "/api/recommendations?limit=6", None)])

    def test_browse_web_tool_uses_bounded_extract_endpoint(self):
        client = FakeClient()
        result = mcp_server.call_tool("litloom_browse_web", {"urls": ["https://example.org/paper"], "max_chars": 800}, client)
        self.assertEqual(result["id"], "job-fixture")
        self.assertEqual(client.calls, [("POST", "/api/agent-tools/browse-web", {"urls": ["https://example.org/paper"], "max_chars": 800})])

    def test_deepseek_fetch_tool_uses_the_local_agent_api(self):
        client = FakeClient()
        result = mcp_server.call_tool(
            "litloom_fetch_web_page",
            {"url": "https://example.org/paper", "max_chars": 900},
            client,
        )
        self.assertEqual(result["id"], "job-fixture")
        self.assertEqual(
            client.calls,
            [("POST", "/api/agent-tools/fetch-web-page", {"url": "https://example.org/paper", "max_chars": 900})],
        )

    def test_analysis_requires_explicit_consent(self):
        with self.assertRaises(PermissionError):
            mcp_server.call_tool("litloom_start_analysis", {"paper_id": "p1", "kind": "reading_card", "consent": False}, FakeClient())
        client = FakeClient()
        result = mcp_server.call_tool("litloom_start_analysis", {"paper_id": "p1", "kind": "reading_card", "consent": True}, client)
        self.assertEqual(result["status"], "queued")
        self.assertTrue(client.calls[0][2]["background"])

    def test_document_tool_can_request_semantic_hybrid_retrieval(self):
        client = FakeClient()
        result = mcp_server.call_tool("litloom_search_document", {"paper_id": "p1", "query": "food volume", "semantic": True}, client)
        self.assertEqual(result["path"], "/api/papers/p1/pages?q=food+volume&limit=20&semantic=1")

    def test_semantic_index_tool_requires_consent_and_queues_durable_task(self):
        client = FakeClient()
        with self.assertRaises(PermissionError):
            mcp_server.call_tool("litloom_build_semantic_index", {"paper_id": "p1", "consent": False}, client)
        result = mcp_server.call_tool("litloom_build_semantic_index", {"paper_id": "p1", "consent": True}, client)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(client.calls[-1][0], "POST")
        self.assertEqual(client.calls[-1][1], "/api/papers/p1/semantic-index")
        self.assertEqual(client.calls[-1][2], {"consent": True})

    def test_semantic_index_status_reads_local_page_coverage(self):
        client = FakeClient()
        result = mcp_server.call_tool("litloom_semantic_index_status", {"paper_id": "p1"}, client)
        self.assertEqual(result["path"], "/api/papers/p1/semantic-index")

    def test_tool_call_returns_structured_content_and_never_credentials(self):
        client = FakeClient()
        response = mcp_server.dispatch({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "litloom_health", "arguments": {}}}, client)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["status"], "ok")
        self.assertNotIn("api_key", response["result"]["content"][0]["text"].lower())

    def test_content_length_and_newline_framing_are_accepted(self):
        newline = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        self.assertEqual(mcp_server._read_message(newline)["method"], "ping")
        payload = '{"jsonrpc":"2.0","id":2,"method":"ping"}'
        framed = io.BytesIO((f"Content-Length: {len(payload.encode('utf-8'))}\r\n\r\n" + payload).encode("utf-8"))
        self.assertEqual(mcp_server._read_message(framed)["id"], 2)

    def test_protocol_output_is_utf8_even_when_console_encoding_is_not(self):
        class BinaryConsole:
            def __init__(self):
                self.buffer = io.BytesIO()

            def flush(self):
                self.buffer.flush()

        console = BinaryConsole()
        mcp_server._write_message(console, {"text": "中文 − 公式"})
        payload = console.buffer.getvalue().decode("utf-8")
        self.assertIn("中文 − 公式", payload)

    def test_surrogate_unicode_is_repaired_before_utf8_transport(self):
        # Escaped non-BMP characters arrive as a surrogate pair on some JSON
        # clients; a genuinely unpaired unit can also be left by a legacy
        # Windows console. Neither should take down the MCP process.
        pair = chr(0xD83D) + chr(0xDCA1)
        unpaired = chr(0xD800)
        repaired = mcp_server._repair_unicode("论文 " + pair + " " + unpaired)
        self.assertIn("💡", repaired)
        self.assertNotIn(unpaired, repaired)
        class BinaryConsole:
            def __init__(self):
                self.buffer = io.BytesIO()
            def flush(self):
                self.buffer.flush()
        console = BinaryConsole()
        mcp_server._write_message(console, {"text": unpaired})
        self.assertIn("�", console.buffer.getvalue().decode("utf-8"))

    def test_unknown_method_is_protocol_error(self):
        response = mcp_server.dispatch({"jsonrpc": "2.0", "id": 9, "method": "unknown"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_search_status_exposes_screening_phase_after_connector_completion(self):
        snapshot = {
            "status": "partial",
            "summary": {"ai_screening": {"status": "running", "processed_count": 1, "candidate_count": 5}},
        }
        enriched = mcp_server._enrich_search_status(snapshot)
        self.assertEqual(enriched["search_status"], "partial")
        self.assertEqual(enriched["screening_status"], "running")
        self.assertEqual(enriched["workflow_status"], "screening_running")
        self.assertEqual(enriched["screening_progress"]["processed"], 1)

    def test_search_status_compacts_large_candidates_without_hiding_the_results(self):
        snapshot = {
            "id": "run-large",
            "status": "partial",
            "summary": {"result_count": 12, "ai_screening": {"status": "completed", "processed_count": 12}},
            "papers": [
                {
                    "id": f"paper-{index}",
                    "title": f"Food volume estimation study {index}",
                    "abstract": "a" * 5000,
                    "relevance_reason": "b" * 1800,
                    "raw": {"payload": "x" * 12000},
                    "ai_triage": {"label": "direct", "score": 94, "confidence": "high", "reason": "c" * 1000},
                }
                for index in range(12)
            ],
        }
        compact = mcp_server._compact_search_snapshot(snapshot, limit=20)
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(encoded), mcp_server.MAX_RESULT_CHARS)
        self.assertEqual(compact["candidate_total"], 12)
        self.assertEqual(compact["papers_returned"], 12)
        self.assertEqual(len(compact["papers"]), 12)
        self.assertNotIn("raw", compact["papers"][0])
        self.assertTrue(compact["papers"][0]["abstract_truncated"])
        self.assertEqual(compact["papers"][0]["ai_triage"]["label"], "direct")
        self.assertFalse(compact["more_papers_available"])

    def test_search_status_paginates_candidates_and_reports_next_offset(self):
        snapshot = {"status": "success", "papers": [{"id": f"paper-{i}", "title": f"Paper {i}"} for i in range(23)]}
        compact = mcp_server._compact_search_tool_result("litloom_search_status", snapshot, {"offset": 10, "limit": 5})
        self.assertEqual(compact["candidate_total"], 23)
        self.assertEqual(compact["papers_offset"], 10)
        self.assertEqual(compact["papers_returned"], 5)
        self.assertEqual(compact["next_offset"], 15)
        self.assertEqual(compact["papers"][0]["id"], "paper-10")

    def test_library_search_compacts_large_model_answers_but_keeps_metadata(self):
        payload = [{
            "id": "paper-1",
            "title": "Fixture",
            "abstract": "a" * 5000,
            "reading_card": {"research_question": "x" * 100000, "completion_pct": 75, "completed_fields": 8, "field_count": 11},
            "ai_artifacts": [{"id": "artifact-1", "artifact_type": "deep_read", "answer": "x" * 100000}],
        }]
        compact = mcp_server._compact_library_response(payload)
        self.assertEqual(compact[0]["reading_card"]["completion_pct"], 75)
        self.assertNotIn("research_question", compact[0]["reading_card"])
        self.assertTrue(compact[0]["abstract_truncated"])
        self.assertLessEqual(len(compact[0]["abstract"]), 4001)
        self.assertEqual(compact[0]["ai_artifacts"][0]["id"], "artifact-1")
        self.assertTrue(compact[0]["ai_artifacts"][0]["answer_available"])
        self.assertNotIn("answer", compact[0]["ai_artifacts"][0])
        self.assertLess(len(json.dumps(compact, ensure_ascii=False)), mcp_server.MAX_RESULT_CHARS)


if __name__ == "__main__":
    unittest.main()
