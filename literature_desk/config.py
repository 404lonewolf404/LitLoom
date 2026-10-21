"""Centralized runtime configuration and startup diagnostics.

The application is intentionally dependency-light, so configuration is kept in
one typed module instead of introducing a settings framework.  Domain modules
should receive values from the application facade and must not parse secrets or
environment variables themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
from typing import Mapping, MutableMapping


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default) or "").strip()


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _text(env, name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _text(env, name, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = _text(env, name, "1" if default else "0").casefold()
    return raw in {"1", "true", "yes", "on"}


def load_env_file(
    path: Path,
    env: MutableMapping[str, str] | None = None,
) -> int:
    """Load simple ``KEY=VALUE`` entries without overriding process env.

    The return value is the number of entries loaded. Shell expansion is
    intentionally unsupported so startup behavior is deterministic on Windows
    and in CI. Secrets are never returned or logged.
    """
    target = env if env is not None else os.environ
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return 0
    loaded = 0
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].lstrip()
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", text)
        if not match:
            continue
        name, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if name not in target:
            target[name] = value
            loaded += 1
    return loaded


@dataclass(frozen=True)
class RuntimeConfig:
    """The stable, operator-facing subset of LitLoom runtime configuration."""

    host: str
    port: int
    data_dir: Path
    database_url_configured: bool
    auth_mode: str
    tenant_mode: str
    release_channel: str
    model_configured: bool
    tavily_configured: bool
    auto_backup_hours: float
    model_concurrency: int
    ai_job_concurrency: int

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        root: Path | None = None,
    ) -> "RuntimeConfig":
        values = env if env is not None else os.environ
        base = (root or Path.cwd()).expanduser().resolve()
        configured_data_dir = _text(values, "LITERATURE_DATA_DIR")
        data_dir = (
            Path(configured_data_dir).expanduser().resolve()
            if configured_data_dir
            else (base / "data").resolve()
        )
        return cls(
            host=_text(values, "LITERATURE_HOST", "127.0.0.1") or "127.0.0.1",
            port=max(1, min(65535, _int(values, "LITERATURE_PORT", 8765))),
            data_dir=data_dir,
            database_url_configured=bool(_text(values, "LITLOOM_DATABASE_URL")),
            auth_mode=_text(values, "LITLOOM_AUTH_MODE", "single") or "single",
            tenant_mode=_text(values, "LITLOOM_TENANT_MODE", "shared") or "shared",
            release_channel=_text(values, "LITLOOM_RELEASE_CHANNEL", "stable")
            or "stable",
            model_configured=bool(
                _text(values, "LIT_AGENT_API_KEY")
                or _text(values, "LIT_AGENT_API_KEY_FILE")
            ),
            tavily_configured=bool(
                _text(values, "LIT_TAVILY_API_KEY")
                or _text(values, "LIT_TAVILY_API_KEY_FILE")
            ),
            auto_backup_hours=max(
                0.0,
                min(8760.0, _float(values, "LITERATURE_AUTO_BACKUP_HOURS", 24.0)),
            ),
            model_concurrency=max(
                1, min(32, _int(values, "LIT_AGENT_MODEL_CONCURRENCY", 4))
            ),
            ai_job_concurrency=max(
                1, min(16, _int(values, "LIT_AGENT_AI_JOB_CONCURRENCY", 2))
            ),
        )

    def validate(self, *, postgres_backend: bool | None = None) -> list[dict[str, str]]:
        """Return actionable configuration findings without exposing secrets."""
        findings: list[dict[str, str]] = []
        try:
            address = ipaddress.ip_address(self.host)
            if not address.is_loopback:
                findings.append(
                    {
                        "severity": "error",
                        "code": "host_not_loopback",
                        "message": "LITERATURE_HOST 必须绑定本机回环地址。",
                    }
                )
        except ValueError:
            findings.append(
                {
                    "severity": "error",
                    "code": "invalid_host",
                    "message": "LITERATURE_HOST 不是有效的 IP 地址。",
                }
            )
        if self.auth_mode.casefold() not in {"single", "team", "shared"}:
            findings.append(
                {
                    "severity": "error",
                    "code": "invalid_auth_mode",
                    "message": "LITLOOM_AUTH_MODE 只能是 single 或 team。",
                }
            )
        if self.tenant_mode.casefold() not in {"shared", "isolated", "tenant", "per_workspace"}:
            findings.append(
                {
                    "severity": "error",
                    "code": "invalid_tenant_mode",
                    "message": "LITLOOM_TENANT_MODE 不是受支持的模式。",
                }
            )
        if postgres_backend is True and self.tenant_mode.casefold() in {
            "isolated",
            "tenant",
            "per_workspace",
        }:
            findings.append(
                {
                    "severity": "error",
                    "code": "postgres_tenant_mode_unsupported",
                    "message": "当前 PostgreSQL 运行版只支持 shared 工作区模式。",
                }
            )
        if self.release_channel.casefold() not in {"stable", "canary", "development"}:
            findings.append(
                {
                    "severity": "warning",
                    "code": "unknown_release_channel",
                    "message": "未知发布通道将按 stable 处理。",
                }
            )
        if self.auto_backup_hours == 0:
            findings.append(
                {
                    "severity": "warning",
                    "code": "backup_disabled",
                    "message": "自动备份已关闭，请确认这符合当前部署策略。",
                }
            )
        if not self.database_url_configured:
            findings.append(
                {
                    "severity": "warning",
                    "code": "database_url_missing",
                    "message": "未设置 LITLOOM_DATABASE_URL，将使用兼容后端或本地默认配置。",
                }
            )
        if not self.model_configured:
            findings.append(
                {
                    "severity": "warning",
                    "code": "model_not_configured",
                    "message": "未配置模型密钥；检索、导入和本地阅读功能仍可使用，AI 功能会被禁用。",
                }
            )
        return findings

    def snapshot(self, *, postgres_backend: bool | None = None) -> dict:
        """Return a safe JSON-serializable snapshot for operators and UI."""
        findings = self.validate(postgres_backend=postgres_backend)
        errors = sum(item["severity"] == "error" for item in findings)
        warnings = sum(item["severity"] == "warning" for item in findings)
        return {
            "status": "invalid" if errors else ("warning" if warnings else "ok"),
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "database": {
                "backend": (
                    "postgresql"
                    if postgres_backend is True
                    else ("sqlite_compat" if postgres_backend is False else "unknown")
                ),
                "url_configured": self.database_url_configured,
            },
            "auth_mode": self.auth_mode,
            "tenant_mode": self.tenant_mode,
            "release_channel": self.release_channel,
            "capabilities": {
                "model": self.model_configured,
                "tavily": self.tavily_configured,
                "automatic_backup": self.auto_backup_hours > 0,
            },
            "resource_budget": {
                "model_concurrency": self.model_concurrency,
                "ai_job_concurrency": self.ai_job_concurrency,
            },
            "findings": findings,
        }


def configuration_snapshot(*, root: Path | None = None, postgres_backend: bool | None = None) -> dict:
    """Build the safe configuration snapshot used by diagnostics and scripts."""
    return RuntimeConfig.from_env(root=root).snapshot(postgres_backend=postgres_backend)


__all__ = ["RuntimeConfig", "configuration_snapshot", "load_env_file"]
