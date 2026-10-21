import os
import unittest
from pathlib import Path
from unittest.mock import patch

from literature_desk.config import RuntimeConfig
from literature_desk.errors import AppError, error_payload, status_error_code


class RuntimeConfigTests(unittest.TestCase):
    def test_snapshot_is_safe_and_reports_missing_optional_services(self):
        with patch.dict(
            os.environ,
            {
                "LITERATURE_HOST": "127.0.0.1",
                "LITERATURE_PORT": "8772",
                "LITLOOM_DATABASE_URL": "",
                "LIT_AGENT_API_KEY": "",
                "LIT_AGENT_API_KEY_FILE": "",
            },
            clear=False,
        ):
            snapshot = RuntimeConfig.from_env(root=Path.cwd()).snapshot()
        self.assertEqual(snapshot["host"], "127.0.0.1")
        self.assertEqual(snapshot["port"], 8772)
        self.assertEqual(snapshot["status"], "warning")
        self.assertFalse(snapshot["capabilities"]["model"])
        self.assertNotIn("api_key", str(snapshot).lower())

    def test_invalid_network_binding_is_an_error(self):
        config = RuntimeConfig.from_env(
            {
                "LITERATURE_HOST": "0.0.0.0",
                "LITERATURE_PORT": "8765",
                "LITLOOM_TENANT_MODE": "shared",
            },
            root=Path.cwd(),
        )
        findings = config.validate()
        self.assertIn("host_not_loopback", {item["code"] for item in findings})
        self.assertEqual(config.snapshot()["status"], "invalid")

    def test_postgres_isolated_tenant_mode_is_rejected(self):
        config = RuntimeConfig.from_env(
            {
                "LITERATURE_HOST": "127.0.0.1",
                "LITLOOM_TENANT_MODE": "isolated",
            },
            root=Path.cwd(),
        )
        findings = config.validate(postgres_backend=True)
        self.assertIn(
            "postgres_tenant_mode_unsupported", {item["code"] for item in findings}
        )


class ErrorContractTests(unittest.TestCase):
    def test_app_error_contains_stable_contract(self):
        error = AppError(
            "upstream_timeout",
            "模型服务响应超时",
            status=504,
            retryable=True,
            details={"provider": "fixture"},
        )
        self.assertEqual(
            error.payload(request_id="req-1"),
            {
                "error": "模型服务响应超时",
                "error_code": "upstream_timeout",
                "retryable": True,
                "request_id": "req-1",
                "details": {"provider": "fixture"},
            },
        )

    def test_unexpected_error_does_not_include_traceback(self):
        payload = error_payload(RuntimeError("database password=secret"), 500, request_id="r")
        self.assertEqual(payload["error_code"], "internal_error")
        self.assertEqual(payload["request_id"], "r")
        self.assertNotIn("Traceback", payload["error"])
        self.assertEqual(status_error_code(429), "rate_limited")


if __name__ == "__main__":
    unittest.main()
