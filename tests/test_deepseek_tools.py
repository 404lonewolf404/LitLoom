import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

import app
import literature_desk.providers as providers


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.payload


class DeepSeekToolTests(unittest.TestCase):
    def configured_settings(self):
        return {
            "deepseek_search_enabled": True,
            "deepseek_search_api_url": "https://relay.example/v1/tools/search",
            "deepseek_fetch_api_url": "https://relay.example/v1/tools/fetch",
            "base_url": "https://relay.example/v1",
            "api_key_set": True,
        }

    def test_fetch_uses_configured_endpoint_bearer_auth_and_bounds_markdown(self):
        response = FakeResponse({"title": "Paper page", "content": "# Paper\n" + "evidence " * 300})
        with (
            patch.object(app, "model_settings", return_value=self.configured_settings()),
            patch.object(app, "model_api_key", return_value="fixture-key"),
            patch.object(app, "_public_source_cooldown_remaining", return_value=0),
            patch.object(app, "_public_source_request_policy", return_value=(4, 0)),
            patch.object(app, "urlopen", return_value=response) as request,
        ):
            result = app.fetch_deepseek_page("https://example.org/paper", max_chars=600)

        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://relay.example/v1/tools/fetch")
        self.assertEqual(sent.get_header("Authorization"), "Bearer fixture-key")
        self.assertEqual(json.loads(sent.data), {"url": "https://example.org/paper"})
        self.assertEqual(result["provider"], "DeepSeek Fetch")
        self.assertEqual(result["evidence_scope"], "external_web_page")
        self.assertLessEqual(len(result["content"]), 600)

    def test_fetch_rejects_private_urls_before_network_access(self):
        with (
            patch.object(app, "model_settings", return_value=self.configured_settings()),
            patch.object(app, "model_api_key", return_value="fixture-key"),
            patch.object(app, "urlopen") as request,
            self.assertRaises(ValueError),
        ):
            app.fetch_deepseek_page("http://127.0.0.1/private")
        request.assert_not_called()

    def test_fetch_is_available_through_the_agent_tool_contract(self):
        expected = {"status": "completed", "provider": "DeepSeek Fetch"}
        with patch.object(app, "fetch_deepseek_page", return_value=expected) as fetch:
            result = app.call_agent_tool(
                "fetch_web_page", {"url": "https://example.org/paper", "max_chars": 900}
            )
        self.assertEqual(result, expected)
        fetch.assert_called_once_with("https://example.org/paper", 900)
        self.assertEqual(app.AGENT_TOOL_SPECS["fetch_web_page"]["class"], "Read")
        self.assertTrue(app.AGENT_TOOL_SPECS["fetch_web_page"]["external_data"])

    def test_fetch_rate_limit_opens_cooldown_and_returns_a_useful_error(self):
        endpoint = "https://relay.example/v1/tools/fetch"
        error = HTTPError(endpoint, 429, "rate limited", {}, None)
        with (
            patch.object(app, "model_api_key", return_value="fixture-key"),
            patch.object(app, "_public_source_cooldown_remaining", return_value=0),
            patch.object(app, "_public_source_request_policy", return_value=(4, 0)),
            patch.object(app, "_cooldown_public_source") as cooldown,
            patch.object(app, "urlopen", side_effect=error),
        ):
            with self.assertRaisesRegex(app.PublicSourceCooldownError, "DeepSeek Fetch 触发限流"):
                app._deepseek_tool_post(endpoint, {"url": "https://example.org"}, tool_name="Fetch")
        cooldown.assert_called_once()

    def test_unconfigured_deepseek_is_not_offered_to_search_agent(self):
        settings = {**self.configured_settings(), "deepseek_search_enabled": False}
        with (
            patch.object(app, "model_settings", return_value=settings),
            patch.object(app, "model_api_key", return_value=""),
            patch.object(app, "_tavily_api_key", return_value=""),
        ):
            connectors = app.model_search_available_connectors()
            schema = app._model_search_tool_schema("chat_completions")

        self.assertIn("openalex", connectors)
        self.assertNotIn("deepseek_search", connectors)
        self.assertNotIn("tavily", connectors)
        self.assertNotIn("deepseek_search", schema["function"]["parameters"]["properties"]["sources"]["items"]["enum"])

    def test_configured_deepseek_is_available_to_search_agent(self):
        with (
            patch.object(app, "model_settings", return_value=self.configured_settings()),
            patch.object(app, "model_api_key", return_value="fixture-key"),
            patch.object(app, "_tavily_api_key", return_value=""),
            patch.object(app, "_public_source_cooldown_remaining", return_value=0),
        ):
            connectors = app.model_search_available_connectors()
            schema = app._model_search_tool_schema("chat_completions")
            _decoded, sources, error = app._model_search_tool_arguments(
                {"query": "food volume", "sources": ["deepseek_search"], "limit": 5}, []
            )

        self.assertIn("deepseek_search", connectors)
        self.assertIn("deepseek_search", schema["function"]["parameters"]["properties"]["sources"]["items"]["enum"])
        self.assertEqual(sources, ["deepseek_search"])
        self.assertEqual(error, "")

    def test_deepseek_search_results_keep_provider_score_semantics(self):
        settings = self.configured_settings()
        candidate = {"title": "Food volume estimation", "url": "https://example.org/paper", "score": 0.87}
        verified = {
            **candidate,
            "source": "deepseek_search",
            "verification_status": "unverified",
            "discovery_only": True,
            "tavily_score": 0.87,
        }
        with (
            patch.object(app, "model_settings", return_value=settings),
            patch.object(app, "model_api_key", return_value="fixture-key"),
            patch.object(providers, "_deepseek_tool_post", return_value={"results": [{
                "title": candidate["title"],
                "url": candidate["url"],
                "summary": "Estimate food volume from images.",
                "rerank_score": 0.87,
            }]}),
            patch.object(app, "_verify_tavily_result", return_value=verified),
        ):
            result = app.search_deepseek("food volume estimation", 5)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "deepseek_search")
        self.assertEqual(result[0]["discovery_score"], 0.87)
        self.assertNotIn("tavily_score", result[0])


if __name__ == "__main__":
    unittest.main()
