"""Model configuration, gateway calls, and model telemetry adapters."""

from __future__ import annotations
from pathlib import Path
from urllib.error import HTTPError, URLError
from contextlib import contextmanager
import sqlite3

from .services import app_services as _services


def _model_request_slot():
    """Return the process-wide model request budget.

    The semaphore is intentionally lazy so importing the module remains cheap
    for tests and command-line maintenance tasks. A slot covers one complete
    HTTP request, including a compatibility retry, and is released even when
    a provider raises or the user cancels the job.
    """
    with _services().MODEL_REQUEST_SEMAPHORE_LOCK:
        if _services().MODEL_REQUEST_SEMAPHORE is None:
            _services().MODEL_REQUEST_SEMAPHORE = (
                _services().threading.BoundedSemaphore(_services().MODEL_CONCURRENCY)
            )
        return _services().MODEL_REQUEST_SEMAPHORE


def _pdf_render_slot():
    """Return the bounded PDF renderer budget used by visual-page workers."""
    with _services().PDF_RENDER_SEMAPHORE_LOCK:
        if _services().PDF_RENDER_SEMAPHORE is None:
            _services().PDF_RENDER_SEMAPHORE = _services().threading.BoundedSemaphore(
                _services().PDF_RENDER_CONCURRENCY
            )
        return _services().PDF_RENDER_SEMAPHORE


def _with_model_request_budget(request):
    slot = _services()._model_request_slot()
    if not slot.acquire(timeout=_services().MODEL_SLOT_TIMEOUT):
        raise TimeoutError(
            f"模型请求排队超过 {_services().MODEL_SLOT_TIMEOUT} 秒；系统已限制并发以保护本机资源"
        )
    try:
        return request()
    finally:
        slot.release()


def cached_derived(key: str, builder, ttl_seconds: float = 12.0):
    """Share expensive local projections across browser tabs.

    The lock intentionally covers the first calculation so simultaneous page
    loads do not stampede SQLite with identical full-library scans.
    """
    now = _services().time.monotonic()
    generation = _services().DERIVED_CACHE_GENERATION
    with _services().DERIVED_CACHE_LOCK:
        cached = _services().DERIVED_CACHE.get(key)
        if cached and now - cached[0] < ttl_seconds:
            return cached[1]
        value = builder()
        if generation == _services().DERIVED_CACHE_GENERATION:
            _services().DERIVED_CACHE[key] = (_services().time.monotonic(), value)
        return value


def invalidate_derived_cache() -> None:
    """Drop recommendation/briefing projections after library changes."""
    _services().DERIVED_CACHE_GENERATION += 1
    with _services().DERIVED_CACHE_LOCK:
        _services().DERIVED_CACHE.clear()
        # Force the next projection to observe a write immediately. Reads
        # still use a tiny debounce window to avoid WAL metadata stampedes.
        _services().DATABASE_TOKEN_CACHE["at"] = 0.0


def database_change_token() -> str:
    """Read database/WAL metadata so external SQLite writes invalidate cache."""
    # Opening a new WAL connection can itself touch the WAL metadata. A
    # short shared token window prevents simultaneous read requests from
    # turning that harmless metadata change into duplicate full-library work.
    now = _services().time.monotonic()
    with _services().DERIVED_CACHE_LOCK:
        if (
            _services().DATABASE_TOKEN_CACHE["value"]
            and now - _services().DATABASE_TOKEN_CACHE["at"] < 5.0
        ):
            return _services().DATABASE_TOKEN_CACHE["value"]
        if _services().using_postgres():
            try:
                with _services().connect() as db:
                    token = str(
                        db.execute("SELECT txid_current_snapshot()::text").fetchone()[0]
                    )
            except Exception:
                token = "postgresql-unavailable"
            _services().DATABASE_TOKEN_CACHE.update(value=token, at=now)
            return token
        try:
            database_stat = _services().DB_PATH.stat()
            wal_path = _services().Path(str(_services().DB_PATH) + "-wal")
            wal_stat = wal_path.stat() if wal_path.exists() else None
            token = f"{database_stat.st_mtime_ns}:{database_stat.st_size}:{wal_stat.st_mtime_ns if wal_stat else 0}:{wal_stat.st_size if wal_stat else 0}"
        except OSError:
            token = "0"
        _services().DATABASE_TOKEN_CACHE.update(value=token, at=now)
        return token


def now_iso() -> str:
    return (
        _services()
        .datetime.now(_services().timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1))))
    return round(float(ordered[rank]), 2)


def record_runtime_request(
    method: str, path: str, status: int, duration_ms: float
) -> None:
    """Record bounded process metrics for local SLOs without persisting payloads."""
    route = _services().clean_text(path.split("?", 1)[0], 180)
    key = f"{_services().clean_text(method, 12).upper()} {route}"
    with _services().RUNTIME_METRICS_LOCK:
        _services().RUNTIME_METRICS["requests_total"] += 1
        _services().RUNTIME_METRICS["errors_4xx"] += int(400 <= status < 500)
        _services().RUNTIME_METRICS["errors_5xx"] += int(status >= 500)
        _services().RUNTIME_METRICS["last_request_at"] = _services().now_iso()
        durations = _services().RUNTIME_METRICS["durations_ms"]
        durations.append(round(max(0.0, float(duration_ms)), 2))
        if len(durations) > 500:
            del durations[:-500]
        route_metrics = (
            _services()
            .RUNTIME_METRICS["routes"]
            .setdefault(key, {"requests": 0, "errors": 0, "last_status": status})
        )
        route_metrics["requests"] += 1
        route_metrics["errors"] += int(status >= 400)
        route_metrics["last_status"] = status


def runtime_metrics_summary() -> dict:
    """Return an operator-safe snapshot of the current process performance window."""
    with _services().RUNTIME_METRICS_LOCK:
        snapshot = {
            "window_started_at": _services().RUNTIME_METRICS["window_started_at"],
            "requests_total": _services().RUNTIME_METRICS["requests_total"],
            "errors_4xx": _services().RUNTIME_METRICS["errors_4xx"],
            "errors_5xx": _services().RUNTIME_METRICS["errors_5xx"],
            "last_request_at": _services().RUNTIME_METRICS["last_request_at"],
            "durations_ms": list(_services().RUNTIME_METRICS["durations_ms"]),
            "routes": {
                key: dict(value)
                for key, value in _services().RUNTIME_METRICS["routes"].items()
            },
        }
    durations = snapshot.pop("durations_ms")
    total = snapshot["requests_total"]
    snapshot.update(
        {
            "sample_size": len(durations),
            "p50_ms": _services()._percentile(durations, 50),
            "p95_ms": _services()._percentile(durations, 95),
            "error_rate_pct": (
                round(
                    ((snapshot["errors_4xx"] + snapshot["errors_5xx"]) / total) * 100, 2
                )
                if total
                else 0
            ),
            "server_error_rate_pct": (
                round((snapshot["errors_5xx"] / total) * 100, 2) if total else 0
            ),
            "route_count": len(snapshot["routes"]),
            "top_routes": sorted(
                ({"route": key, **value} for key, value in snapshot["routes"].items()),
                key=lambda item: (-item["requests"], item["route"]),
            )[:20],
            "ai_resource_budget": {
                "model_concurrency": _services().MODEL_CONCURRENCY,
                "ai_job_concurrency": _services().AI_JOB_CONCURRENCY,
                "fulltext_concurrency": _services().FULLTEXT_CONCURRENCY,
                "pdf_render_concurrency": _services().PDF_RENDER_CONCURRENCY,
                "pdf_download_concurrency": _services().PDF_DOWNLOAD_CONCURRENCY,
                "ocr_concurrency": _services().OCR_CONCURRENCY,
                "model_slot_timeout_seconds": _services().MODEL_SLOT_TIMEOUT,
                "model_progress_heartbeat_seconds": _services().MODEL_PROGRESS_HEARTBEAT_SECONDS,
                "model_output_token_cap": _services().MODEL_MAX_OUTPUT_TOKENS,
                "analysis_reasoning_effort": _services().MODEL_ANALYSIS_REASONING_EFFORT
                or "provider_default",
                "synthesis_reasoning_effort": _services().MODEL_REASONING_EFFORT
                or "provider_default",
                "reading_card_output_token_cap": _services().READING_CARD_SYNTHESIS_MAX_OUTPUT_TOKENS,
                "reading_card_context_chars": _services().READING_CARD_CONTEXT_CHARS,
                "reading_card_visual_pages_per_chunk": _services().READING_CARD_VISUAL_PAGES_PER_CHUNK,
            },
        }
    )
    try:
        started = _services().datetime.fromisoformat(snapshot["window_started_at"])
        snapshot["uptime_seconds"] = max(
            0,
            round(
                (
                    _services().datetime.now(_services().timezone.utc) - started
                ).total_seconds()
            ),
        )
    except (TypeError, ValueError):
        snapshot["uptime_seconds"] = 0
    snapshot["model_usage"] = _services().model_usage_summary(30)
    return snapshot


def local_model_key_path() -> Path:
    """Return the local secret path without baking DATA_DIR at import time.

    ``LIT_AGENT_API_KEY_FILE`` is useful for a protected location outside the
    workspace.  Otherwise the key lives beside the local SQLite data, which
    survives source updates while remaining outside the tracked code tree.
    """
    configured = _services().os.environ.get("LIT_AGENT_API_KEY_FILE", "").strip()
    return (
        _services().Path(configured).expanduser()
        if configured
        else _services().DATA_DIR / "model-secret.json"
    )


def read_local_model_key() -> str:
    """Read a local API key from JSON or a plain-text secret file.

    Plain text is accepted so existing secret-manager exports can be pointed at
    the application without conversion.  The file is deliberately size
    bounded and its contents never leave this process.
    """
    path = _services().local_model_key_path()
    try:
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            return ""
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""
    if not raw:
        return ""
    try:
        value = _services().json.loads(raw)
        if isinstance(value, dict):
            value = value.get(
                "api_key", value.get("key", value.get("LIT_AGENT_API_KEY", ""))
            )
        if not isinstance(value, str):
            return ""
        return value.strip()
    except (TypeError, ValueError):
        return raw


def write_local_model_key(api_key: str) -> None:
    """Atomically persist a key with restrictive permissions where supported."""
    path = _services().local_model_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _services().json.dumps(
        {"api_key": api_key, "updated_at": _services().now_iso()}, ensure_ascii=False
    )
    temporary = None
    try:
        with _services().tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = _services().Path(handle.name)
            handle.write(payload)
            handle.flush()
            _services().os.fsync(handle.fileno())
        try:
            _services().os.chmod(temporary, 0o600)
        except OSError:
            pass
        _services().os.replace(temporary, path)
        try:
            _services().os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def clear_local_model_key() -> None:
    """Remove only the configured local secret file."""
    try:
        _services().local_model_key_path().unlink(missing_ok=True)
    except OSError:
        pass


def model_api_key_details() -> tuple[str, str]:
    """Resolve the key and its source without exposing the key to callers."""
    if _services().RUNTIME_MODEL_KEY:
        return _services().RUNTIME_MODEL_KEY, "当前进程"
    local_key = _services().read_local_model_key()
    if local_key:
        return local_key, "本地配置文件"
    env_key = _services().os.environ.get("LIT_AGENT_API_KEY", "").strip()
    if env_key:
        return env_key, "环境变量"
    return "", "未配置"


def model_api_key() -> str:
    """Resolve the configured key for model calls."""
    return _services().model_api_key_details()[0]


def model_provider_presets() -> list[dict]:
    """Return safe, editable provider presets without secrets."""
    return [dict(item) for item in _services().MODEL_PROVIDER_PRESETS]


def model_endpoint_warning(api_url: str, protocol: str) -> str:
    """Explain an obvious endpoint/protocol mismatch without changing config."""
    endpoint = str(api_url or "").rstrip("/").casefold()
    if not endpoint:
        return ""
    if endpoint.endswith("/responses") and protocol != "responses":
        return "API 地址以 /responses 结尾，但协议选择了 Chat Completions；请保持两者一致。"
    if endpoint.endswith("/chat/completions") and protocol == "responses":
        return "API 地址以 /chat/completions 结尾，但协议选择了 Responses；请保持两者一致。"
    return ""


def model_settings() -> dict:
    """Return OpenAI-compatible settings without ever returning the secret."""
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute("SELECT * FROM model_config WHERE id=1").fetchone()
    env_key = _services().os.environ.get("LIT_AGENT_API_KEY", "")
    has_saved = bool(row and row["updated_at"])
    # Do not silently bind an unconfigured installation to the OpenAI domain.
    # A provider may expose only a custom inference URL, in which case the
    # derived base is useful for display and legacy diagnostics.
    base_url = (
        row["base_url"]
        if has_saved and row["base_url"]
        else _services().os.environ.get("LIT_AGENT_BASE_URL", "")
    ).rstrip("/")
    website_url = (
        row["website_url"]
        if has_saved and "website_url" in row.keys() and row["website_url"]
        else ""
    )
    api_url = (
        row["api_url"]
        if has_saved and "api_url" in row.keys() and row["api_url"]
        else _services().os.environ.get("LIT_AGENT_API_URL", "")
    ).rstrip("/")
    if not base_url and api_url:
        base_url = _services().model_base_url(api_url)
    protocol = (
        (
            row["protocol"]
            if has_saved and "protocol" in row.keys() and row["protocol"]
            else _services().os.environ.get("LIT_AGENT_PROTOCOL", "chat_completions")
        )
        .strip()
        .lower()
    )
    if protocol not in {"chat_completions", "responses"}:
        protocol = "chat_completions"
    model = (
        row["model"]
        if has_saved and row["model"]
        else _services().os.environ.get("LIT_AGENT_MODEL", "")
    )
    embedding_model = (
        row["embedding_model"] if has_saved and "embedding_model" in row.keys() else ""
    ) or _services().os.environ.get("LIT_AGENT_EMBEDDING_MODEL", "")
    embedding_api_url = (
        row["embedding_api_url"]
        if has_saved and "embedding_api_url" in row.keys()
        else ""
    ) or _services().os.environ.get("LIT_AGENT_EMBEDDING_API_URL", "")
    provider_name = row["provider"] if has_saved and row["provider"] else "自定义"
    configured_search_enabled = bool(
        row["deepseek_search_enabled"]
        if has_saved and "deepseek_search_enabled" in row.keys()
        else False
    )
    env_search_enabled = _services().os.environ.get("LIT_DEEPSEEK_SEARCH_ENABLED", "").strip().lower()
    deepseek_host = (_services().urlparse(base_url or api_url).hostname or "").casefold()
    if env_search_enabled in {"0", "false", "no", "off"}:
        deepseek_search_enabled = False
    elif env_search_enabled in {"1", "true", "yes", "on"}:
        deepseek_search_enabled = True
    elif has_saved and "deepseek_search_enabled" in row.keys():
        deepseek_search_enabled = configured_search_enabled
    else:
        deepseek_search_enabled = provider_name.casefold() == "deepseek" or deepseek_host == "api.deepseek.com"
    deepseek_search_api_url = (
        row["deepseek_search_api_url"]
        if has_saved and "deepseek_search_api_url" in row.keys() and row["deepseek_search_api_url"]
        else _services().os.environ.get("LIT_DEEPSEEK_SEARCH_API_URL", "")
    ).rstrip("/")
    deepseek_fetch_api_url = (
        row["deepseek_fetch_api_url"]
        if has_saved and "deepseek_fetch_api_url" in row.keys() and row["deepseek_fetch_api_url"]
        else _services().os.environ.get("LIT_DEEPSEEK_FETCH_API_URL", "")
    ).rstrip("/")
    fast_model = (
        row["fast_model"] if has_saved and "fast_model" in row.keys() else ""
    ) or _services().os.environ.get("LIT_AGENT_FAST_MODEL", "")
    analysis_model = (
        row["analysis_model"] if has_saved and "analysis_model" in row.keys() else ""
    ) or _services().os.environ.get("LIT_AGENT_ANALYSIS_MODEL", "")
    vision_model = (
        row["vision_model"] if has_saved and "vision_model" in row.keys() else ""
    ) or _services().os.environ.get("LIT_AGENT_VISION_MODEL", "")
    effective_embedding_api_url = embedding_api_url or (
        base_url.rstrip("/") + "/embeddings" if embedding_model and base_url else ""
    )
    temperature = float(row["temperature"] if row else 0.2)
    key, key_source = _services().model_api_key_details()
    enabled = bool(row["enabled"]) if has_saved else bool(key)
    provider = provider_name
    # Report the effective local secret file rather than only the optional
    # environment override.  This keeps the settings panel truthful after a
    # restart when the default data/model-secret.json is being used.
    try:
        key_file_configured = _services().local_model_key_path().is_file()
    except OSError:
        key_file_configured = False
    return {
        "provider": provider,
        "base_url": base_url,
        "website_url": website_url,
        "api_url": api_url,
        "protocol": protocol,
        "endpoint_warning": _services().model_endpoint_warning(api_url, protocol),
        "model": model,
        "fast_model": fast_model,
        "analysis_model": analysis_model,
        "vision_model": vision_model,
        "temperature": temperature,
        "embedding_model": embedding_model,
        "embedding_api_url": embedding_api_url,
        "deepseek_search_enabled": deepseek_search_enabled,
        "deepseek_search_api_url": deepseek_search_api_url,
        "deepseek_fetch_api_url": deepseek_fetch_api_url,
        "effective_embedding_api_url": effective_embedding_api_url,
        "embedding_configured": bool(embedding_model and effective_embedding_api_url),
        "enabled": enabled,
        "api_key_set": bool(key),
        "api_key_source": key_source,
        "api_key_file_configured": key_file_configured,
        "updated_at": row["updated_at"] if row else "",
    }


def save_model_settings(data: dict) -> dict:
    provider = str(data.get("provider", "自定义")).strip()
    provider = _services().clean_text(provider, 120) or "自定义"
    supplied_api_key = str(data.get("api_key") or "").strip()
    base_url = str(data.get("base_url", "")).strip().rstrip("/")
    website_url = str(data.get("website_url", "")).strip().rstrip("/")
    if website_url:
        site = _services().urlparse(website_url)
        if (
            site.scheme not in {"http", "https"}
            or not site.netloc
            or site.username
            or site.password
            or site.query
            or site.fragment
        ):
            raise ValueError("官网链接必须是完整的 http(s) 地址")
    protocol = str(data.get("protocol", "chat_completions")).strip().lower()
    if protocol not in {"chat_completions", "responses"}:
        raise ValueError("协议必须是 Chat Completions 或 Responses")
    api_url = str(data.get("api_url", "")).strip().rstrip("/")
    if api_url:
        endpoint = _services().urlparse(api_url)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.netloc
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("API 请求地址必须是完整的 http(s) 地址")
        if endpoint.scheme == "http" and endpoint.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("非本机 API 服务请使用 HTTPS")
    with _services().DB_LOCK, _services().db_session() as db:
        current_embedding = db.execute(
            "SELECT embedding_model,embedding_api_url,fast_model,analysis_model,vision_model,deepseek_search_enabled,deepseek_search_api_url,deepseek_fetch_api_url FROM model_config WHERE id=1"
        ).fetchone()
    embedding_model = _services().clean_text(
        data.get(
            "embedding_model",
            current_embedding["embedding_model"] if current_embedding else "",
        ),
        200,
    )
    embedding_api_url = (
        str(
            data.get(
                "embedding_api_url",
                current_embedding["embedding_api_url"] if current_embedding else "",
            )
        )
        .strip()
        .rstrip("/")
    )
    route_models = {
        name: _services().clean_text(
            data.get(name, current_embedding[name] if current_embedding else ""), 200
        )
        for name in ("fast_model", "analysis_model", "vision_model")
    }
    deepseek_search_enabled = int(bool(data.get(
        "deepseek_search_enabled",
        current_embedding["deepseek_search_enabled"] if current_embedding and "deepseek_search_enabled" in current_embedding.keys() else False,
    )))
    deepseek_search_api_url = _services().clean_text(data.get(
        "deepseek_search_api_url",
        current_embedding["deepseek_search_api_url"] if current_embedding and "deepseek_search_api_url" in current_embedding.keys() else "",
    ), 500).rstrip("/")
    deepseek_fetch_api_url = _services().clean_text(data.get(
        "deepseek_fetch_api_url",
        current_embedding["deepseek_fetch_api_url"] if current_embedding and "deepseek_fetch_api_url" in current_embedding.keys() else "",
    ), 500).rstrip("/")
    for label, value in (("DeepSeek Search", deepseek_search_api_url), ("DeepSeek Fetch", deepseek_fetch_api_url)):
        if not value:
            continue
        endpoint = _services().urlparse(value)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError(f"{label} 地址必须是完整的 http(s) 地址，且不能包含密钥或查询参数")
        if endpoint.scheme == "http" and endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(f"非本机 {label} 服务请使用 HTTPS")
    if embedding_api_url:
        endpoint = _services().urlparse(embedding_api_url)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.netloc
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError(
                "Embedding API 地址必须是完整的 http(s) 地址，且不能包含密钥或查询参数"
            )
        if endpoint.scheme == "http" and endpoint.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("非本机 Embedding 服务请使用 HTTPS")
    if not base_url and api_url:
        base_url = _services().model_base_url(api_url)
    if not base_url and not api_url:
        raise ValueError("请至少填写 Base URL 或 API 请求地址其中一个")
    if base_url:
        parsed = _services().urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Base URL 必须是完整的 http(s) 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Base URL 不要包含用户名、密码、查询参数或密钥；请把密钥填写在 API Key 中"
            )
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("非本机服务请使用 HTTPS")
    model = _services().clean_text(data.get("model", ""), 200)
    if not model:
        raise ValueError("请填写模型 ID")
    try:
        temperature = min(2.0, max(0.0, float(data.get("temperature", 0.2))))
    except (TypeError, ValueError):
        raise ValueError("temperature 必须是 0 到 2 之间的数字")
    explicit_key_clear = (
        "api_key" in data and not supplied_api_key and bool(data.get("clear_api_key"))
    )
    if _services().model_api_key() and not supplied_api_key and not explicit_key_clear:
        with _services().DB_LOCK, _services().db_session() as db:
            current = db.execute(
                "SELECT provider FROM model_config WHERE id=1"
            ).fetchone()
        current_provider = (
            _services().clean_text(current["provider"], 120) if current else "自定义"
        )
        if current_provider.casefold() != provider.casefold():
            raise ValueError(
                "切换模型供应商时请填写该服务的新 API Key，避免把当前密钥发送到其他服务"
            )
    if "api_key" in data:
        if supplied_api_key:
            _services().RUNTIME_MODEL_KEY = supplied_api_key
            if data.get("persist_api_key", True) is not False:
                _services().write_local_model_key(supplied_api_key)
            else:
                _services().clear_local_model_key()
        elif data.get("clear_api_key"):
            _services().RUNTIME_MODEL_KEY = ""
            _services().clear_local_model_key()
    enabled = int(bool(data.get("enabled", True)))
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "UPDATE model_config SET provider=?,base_url=?,website_url=?,api_url=?,protocol=?,model=?,fast_model=?,analysis_model=?,vision_model=?,temperature=?,embedding_model=?,embedding_api_url=?,deepseek_search_enabled=?,deepseek_search_api_url=?,deepseek_fetch_api_url=?,enabled=?,updated_at=? WHERE id=1",
            (
                provider,
                base_url,
                website_url,
                api_url,
                protocol,
                model,
                route_models["fast_model"],
                route_models["analysis_model"],
                route_models["vision_model"],
                temperature,
                embedding_model,
                embedding_api_url,
                deepseek_search_enabled,
                deepseek_search_api_url,
                deepseek_fetch_api_url,
                enabled,
                _services().now_iso(),
            ),
        )
        _services().record_audit(
            db,
            "model.settings.updated",
            "model_config",
            "1",
            {
                "provider": provider,
                "base_url": base_url,
                "website_url": website_url,
                "api_url": api_url,
                "protocol": protocol,
                "model": model,
                **route_models,
                "embedding_model": embedding_model,
                "embedding_api_url": embedding_api_url,
                "deepseek_search_enabled": bool(deepseek_search_enabled),
                "deepseek_search_api_url": deepseek_search_api_url,
                "deepseek_fetch_api_url": deepseek_fetch_api_url,
                "enabled": bool(enabled),
            },
        )
    return _services().model_settings()


def read_model_first_token(response, timeout: int | None = None) -> tuple[str, bool]:
    """Return the first generated text fragment, or the full non-stream reply."""
    if timeout is None:
        timeout = _services().MODEL_PROBE_TIMEOUT
    content_type = (
        response.headers.get("Content-Type", "")
        if getattr(response, "headers", None)
        else ""
    ).lower()
    if "event-stream" not in content_type:
        result = _services().json.loads(response.read().decode("utf-8", "replace"))
        return _services().model_response_content(result).strip(), False

    deadline = _services().time.monotonic() + timeout
    for raw_line in response:
        if _services().time.monotonic() >= deadline:
            raise TimeoutError(f"模型在 {timeout} 秒内没有开始输出")
        line = (
            raw_line.decode("utf-8", "replace")
            if isinstance(raw_line, bytes)
            else str(raw_line)
        )
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = _services().json.loads(data)
        except (TypeError, ValueError):
            continue
        delta = event.get("delta", "")
        if isinstance(delta, dict):
            delta = delta.get("text", "")
        if not isinstance(delta, str) or not delta:
            choices = event.get("choices") or []
            if choices and isinstance(choices[0], dict):
                # DeepSeek reasoning models can emit only reasoning_content
                # in the first SSE event. That is still proof that inference
                # has started and must not be reported as an empty response.
                choice_delta = choices[0].get("delta") or {}
                if isinstance(choice_delta, dict):
                    delta = (
                        choice_delta.get("content")
                        or choice_delta.get("reasoning_content")
                        or choice_delta.get("reasoning")
                        or choice_delta.get("text")
                        or ""
                    )
        if isinstance(delta, str) and delta.strip():
            return delta.strip(), True
        if event.get("type") in {"response.output_text.done", "response.completed"}:
            completed = event.get("response", event)
            text = (
                _services().model_response_content(completed).strip()
                if isinstance(completed, dict)
                else ""
            )
            if text:
                return text, True
    return "", True


def test_model_connection(deep: bool = False) -> dict:
    started = _services().time.perf_counter()
    settings = _services().model_settings()
    if not settings["api_key_set"]:
        raise ValueError("尚未配置 API Key")
    # A quick catalog probe is used by the UI when the provider exposes
    # /models. Callers can request deep=True for an actual generation probe.
    if not deep and settings.get("base_url"):
        models_url = settings["base_url"] + "/models"
        req = _services().Request(
            models_url,
            headers={
                "Authorization": "Bearer " + _services().model_api_key(),
                "Accept": "application/json",
                "User-Agent": _services().USER_AGENT,
            },
        )
        try:
            with _services().urlopen(req, timeout=8) as response:
                if response.status < 200 or response.status >= 300:
                    raise ValueError(
                        f"模型服务返回 HTTP {response.status}（GET {models_url}）"
                    )
        except _services().HTTPError as exc:
            # A number of compatible relays intentionally omit /models. In
            # that case continue to the bounded inference probe below.
            if exc.code in {404, 405, 501}:
                pass
            else:
                detail = ""
                api_key = _services().model_api_key()
                try:
                    detail = exc.read(300).decode("utf-8", "replace").strip()
                except Exception:
                    pass
                if api_key:
                    detail = detail.replace(api_key, "[已隐藏]")
                suffix = f"：{detail[:180]}" if detail else ""
                raise ValueError(
                    f"模型服务连接失败：HTTP {exc.code}（GET {models_url}）{suffix}"
                ) from exc
        except (_services().URLError, TimeoutError, OSError) as exc:
            # If a service root is unreachable, do not hide that actionable
            # network error behind a second, slower inference request.
            raise ValueError(f"模型服务连接失败：{str(exc)[:240]}") from exc
        else:
            return {
                "ok": True,
                "base_url": settings["base_url"],
                "api_url": settings.get("api_url", ""),
                "protocol": settings.get("protocol", "chat_completions"),
                "model": settings["model"],
                "checked_at": _services().now_iso(),
                "tested": "models",
                "duration_ms": round(
                    (_services().time.perf_counter() - started) * 1000, 2
                ),
                "stream": False,
            }
    api_key = _services().model_api_key()
    payload = (
        {
            "model": settings["model"],
            "instructions": "Output only the token 1.",
            "input": "1",
            "max_output_tokens": 1,
            "stream": True,
            "store": False,
        }
        if settings.get("protocol") == "responses"
        else {
            "model": settings["model"],
            "messages": [{"role": "user", "content": "Output only the token 1."}],
            "max_tokens": 1,
            "stream": True,
        }
    )
    if _services().MODEL_PROBE_REASONING:
        if settings.get("protocol") == "responses":
            payload["reasoning"] = {"effort": _services().MODEL_PROBE_REASONING}
        else:
            payload["reasoning_effort"] = _services().MODEL_PROBE_REASONING
    inference_url = _services().model_endpoint(settings)
    probe_started_perf = _services().time.perf_counter()
    probe_attempts = 0
    probe_usage_recorded = False
    try:
        output = ""
        streamed = False
        first_token_ms = None
        for _attempt in range(5):
            probe_attempts += 1
            request_started = _services().time.perf_counter()
            inference_req = _services().Request(
                inference_url,
                data=_services().json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + api_key,
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream, application/json",
                    "User-Agent": _services().USER_AGENT,
                },
                method="POST",
            )
            try:
                with _services().urlopen(
                    inference_req, timeout=_services().MODEL_PROBE_TIMEOUT
                ) as response:
                    output, streamed = _services().read_model_first_token(response)
                first_token_ms = round(
                    (_services().time.perf_counter() - request_started) * 1000, 2
                )
                usage_metadata = _services()._model_usage_metadata(
                    inference_url, payload, probe_attempts
                )
                usage_metadata.update(
                    task="connection_test", started_perf=probe_started_perf
                )
                _services().record_model_usage(usage_metadata)
                probe_usage_recorded = True
                break
            except _services().HTTPError as exc:
                detail = _services().model_error_detail(exc, api_key)
                folded = detail.casefold()
                compatible_retry = False
                if exc.code == 400 and "store" in folded and "store" in payload:
                    payload.pop("store", None)
                    compatible_retry = True
                elif (
                    exc.code == 400
                    and (
                        "reasoning" in folded
                        or "unknown parameter" in folded
                        or "extra fields" in folded
                    )
                    and ("reasoning" in payload or "reasoning_effort" in payload)
                ):
                    payload.pop("reasoning", None)
                    payload.pop("reasoning_effort", None)
                    compatible_retry = True
                elif (
                    exc.code == 400
                    and payload.get("stream")
                    and any(
                        term in folded
                        for term in ("stream", "unknown parameter", "extra fields")
                    )
                ):
                    payload["stream"] = False
                    compatible_retry = True
                elif (
                    exc.code == 400
                    and any(
                        term in folded
                        for term in (
                            "max_tokens",
                            "max_output_tokens",
                            "unknown parameter",
                            "extra fields",
                        )
                    )
                    and ("max_output_tokens" in payload or "max_tokens" in payload)
                ):
                    payload.pop("max_output_tokens", None)
                    payload.pop("max_tokens", None)
                    compatible_retry = True
                if compatible_retry:
                    continue
                raise ValueError(
                    f"模型推理测试失败（POST {inference_url}）：{detail}"
                ) from exc
        else:
            raise ValueError("模型服务多次拒绝兼容参数，无法完成推理测试")
        if not output:
            raise ValueError("模型接口已连接，但没有返回文本内容")
    except (
        _services().URLError,
        TimeoutError,
        OSError,
        _services().json.JSONDecodeError,
        ValueError,
    ) as exc:
        if probe_attempts and not probe_usage_recorded:
            usage_metadata = _services()._model_usage_metadata(
                inference_url, payload, probe_attempts
            )
            usage_metadata.update(
                task="connection_test", started_perf=probe_started_perf
            )
            _services().record_model_usage(usage_metadata, error=exc)
        if isinstance(exc, ValueError) and str(exc).startswith("模型推理测试失败"):
            raise
        raise ValueError(
            f"模型推理测试失败（POST {inference_url}）：{_services().model_error_detail(exc, api_key)}"
        ) from exc
    # A streamed probe intentionally closes as soon as the first generated
    # fragment arrives. Keep the legacy ``response_complete`` field for API
    # compatibility, but expose the probe semantics explicitly so clients do
    # not present a deliberately bounded probe as an incomplete answer.
    first_token_probe = bool(streamed)
    return {
        "ok": True,
        "base_url": settings["base_url"],
        "api_url": settings.get("api_url", ""),
        "inference_url": inference_url,
        "protocol": settings.get("protocol", "chat_completions"),
        "model": settings["model"],
        "checked_at": _services().now_iso(),
        "tested": "inference",
        "duration_ms": round((_services().time.perf_counter() - started) * 1000, 2),
        "time_to_first_token_ms": first_token_ms if streamed else None,
        "stream": streamed,
        "response_complete": not streamed,
        "probe_mode": "first_token" if first_token_probe else "complete_response",
        "probe_completed": True,
        "stream_closed_after_first_token": first_token_probe,
        "reasoning_effort": _services().MODEL_PROBE_REASONING or "provider_default",
        "probe_timeout_seconds": _services().MODEL_PROBE_TIMEOUT,
    }


def model_endpoint(settings: dict) -> str:
    """Resolve the provider's actual generation endpoint."""
    configured = (settings.get("api_url") or "").rstrip("/")
    suffix = (
        "/responses" if settings.get("protocol") == "responses" else "/chat/completions"
    )
    if configured:
        # An explicit API URL is authoritative.  Relays frequently expose
        # non-standard paths, so guessing a suffix here can silently route a
        # valid configuration to a different endpoint.
        return configured
    return settings["base_url"].rstrip("/") + suffix


def add_model_reasoning(
    payload: dict, settings: dict, effort: str | None = None
) -> dict:
    """Bound reasoning for ordinary calls so relays do not silently choose high.

    ``effort`` is optional so latency-sensitive, already-structured passes can
    opt out without changing the user's global model preference.
    """
    selected_effort = (
        _services().MODEL_REASONING_EFFORT
        if effort is None
        else str(effort).strip().lower()
    )
    if not selected_effort:
        return payload
    if settings.get("protocol") == "responses":
        payload["reasoning"] = {"effort": selected_effort}
    else:
        payload["reasoning_effort"] = selected_effort
    return payload


def add_json_output_format(payload: dict, settings: dict) -> dict:
    """Ask compatible model APIs to constrain JSON-producing calls."""
    if settings.get("protocol") == "responses":
        payload["text"] = {"format": {"type": "json_object"}}
    else:
        payload["response_format"] = {"type": "json_object"}
    return payload


def model_base_url(api_url: str) -> str:
    """Derive a displayable service root from a custom inference URL."""
    value = str(api_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if value.endswith(suffix):
            return value[: -len(suffix)].rstrip("/")
    return value


def model_response_content(result: dict) -> str:
    """Read text from Chat Completions and Responses shaped payloads."""
    choices = result.get("choices") or []
    message = (
        choices[0].get("message", {})
        if choices and isinstance(choices[0], dict)
        else {}
    )
    content = message.get("content", "") if isinstance(message, dict) else ""
    if not content and isinstance(message, dict):
        # DeepSeek R1/ V3 reasoning mode may put the only early text in one
        # of these provider-specific fields. Prefer normal content whenever
        # it exists, and use reasoning as a compatibility fallback.
        content = message.get("reasoning_content") or message.get("reasoning") or ""
    if not content:
        content = result.get("output_text", "")
    if not content:
        for item in result.get("output", []) or []:
            for part in item.get("content", []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("text"):
                    content += str(part["text"])
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content or "")


def model_error_detail(exc: Exception, api_key: str = "") -> str:
    """Keep provider diagnostics useful while redacting credentials."""
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    http_error = next(
        (item for item in chain if isinstance(item, _services().HTTPError)), None
    )
    detail = ""
    if http_error:
        detail = getattr(http_error, "_litloom_error_detail", "")
        if not detail:
            try:
                detail = http_error.read(800).decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
        if api_key:
            detail = detail.replace(api_key, "[已隐藏]")
        guidance = {
            401: "API 密钥无效或未获授权",
            402: "模型服务账户额度不足或计费未启用",
            403: "模型服务拒绝访问，请检查账户权限",
            404: "模型或 API 请求地址不存在",
            413: "请求内容超过模型服务限制",
            429: "模型服务触发速率限制，请稍后重试",
            524: "模型中转服务等待超时，可重试或减少单次分析内容",
        }.get(http_error.code, "模型服务请求失败" if http_error.code >= 500 else "模型服务拒绝请求")
        return f"HTTP {http_error.code}（{guidance}）" + (
            f"：{detail[:500]}" if detail else ""
        )
    message = str(exc)
    for item in chain[1:]:
        cause_message = str(item)
        if cause_message and cause_message not in message:
            message += f"；底层原因：{cause_message}"
        if len(message) >= 500:
            break
    if api_key:
        message = message.replace(api_key, "[已隐藏]")
    return message[:500]


def _model_retry_delay(retry_index: int, headers=None) -> float:
    """Return a short provider-aware backoff without trusting huge headers."""
    retry_after = 0.0
    try:
        retry_after = float((headers or {}).get("Retry-After", ""))
    except (AttributeError, TypeError, ValueError):
        pass
    exponential = _services().MODEL_RETRY_BASE_DELAY * (2 ** max(0, retry_index))
    return min(max(retry_after, exponential), _services().MODEL_RETRY_MAX_DELAY)


class PublicSourceCooldownError(URLError):
    """A public index is temporarily unavailable and should be skipped."""


@contextmanager
def fast_model_search_source_budget(timeout: int | None = None):
    """Apply a short, no-retry budget used by model-led discovery threads."""
    previous = bool(getattr(_services().MODEL_SEARCH_FAST_SOURCE, "enabled", False))
    previous_timeout = getattr(_services().MODEL_SEARCH_FAST_SOURCE, "timeout", None)
    _services().MODEL_SEARCH_FAST_SOURCE.enabled = True
    if timeout is not None:
        _services().MODEL_SEARCH_FAST_SOURCE.timeout = max(1, int(timeout))
    try:
        yield
    finally:
        _services().MODEL_SEARCH_FAST_SOURCE.enabled = previous
        if previous_timeout is None:
            try:
                del _services().MODEL_SEARCH_FAST_SOURCE.timeout
            except AttributeError:
                pass
        else:
            _services().MODEL_SEARCH_FAST_SOURCE.timeout = previous_timeout


def _public_source_request_policy(timeout: int) -> tuple[int, int]:
    """Resolve timeout/retry values without changing normal sync behavior."""
    if getattr(_services().MODEL_SEARCH_FAST_SOURCE, "enabled", False):
        fast_timeout = (
            getattr(_services().MODEL_SEARCH_FAST_SOURCE, "timeout", None)
            or _services().MODEL_SEARCH_SOURCE_TIMEOUT
        )
        return min(max(int(timeout or fast_timeout), 1), int(fast_timeout)), 0
    return (
        max(1, min(int(timeout or _services().PUBLIC_SOURCE_TIMEOUT), 60)),
        _services().PUBLIC_SOURCE_RETRIES,
    )


def _public_source_key(url: str) -> str:
    """Return a stable public-index key without retaining query contents."""
    try:
        parsed = _services().urlparse(str(url))
        return (parsed.hostname or parsed.netloc or "public-source").casefold()
    except Exception:
        return "public-source"


def _public_source_request_lock(url: str):
    """Return the host-level gate used by public-source HTTP helpers."""
    key = _services()._public_source_key(url)
    with _services().PUBLIC_SOURCE_REQUEST_LOCKS_LOCK:
        lock = _services().PUBLIC_SOURCE_REQUEST_LOCKS.get(key)
        if lock is None:
            lock = _services().threading.Lock()
            _services().PUBLIC_SOURCE_REQUEST_LOCKS[key] = lock
        return lock


def _public_source_cooldown_remaining(url: str) -> float:
    key = _services()._public_source_key(url)
    now = _services().time.monotonic()
    with _services().PUBLIC_SOURCE_STATE_LOCK:
        expires = float(_services().PUBLIC_SOURCE_COOLDOWNS.get(key, 0.0) or 0.0)
        if expires <= now:
            _services().PUBLIC_SOURCE_COOLDOWNS.pop(key, None)
            return 0.0
        return expires - now


def _load_persisted_public_cooldowns(db: sqlite3.Connection) -> None:
    """Restore non-expired public-index cooldowns from the local database."""
    prefix = _services().PUBLIC_SOURCE_COOLDOWN_META_PREFIX
    now_wall = _services().time.time()
    now_mono = _services().time.monotonic()
    expired: list[tuple[str]] = []
    restored: list[tuple[str, float]] = []
    try:
        rows = db.execute(
            "SELECT key,value FROM app_meta WHERE key LIKE ?",
            (prefix + "%",),
        ).fetchall()
    except _services().sqlite3.Error:
        return
    for row in rows:
        key = str(row["key"] or "")
        host = key.removeprefix(prefix).strip().casefold()
        try:
            expires_wall = float(row["value"] or 0)
        except (TypeError, ValueError):
            expires_wall = 0.0
        remaining = expires_wall - now_wall
        if not host or remaining <= 0:
            expired.append((key,))
            continue
        restored.append((host, now_mono + remaining))
    with _services().PUBLIC_SOURCE_STATE_LOCK:
        _services().PUBLIC_SOURCE_COOLDOWNS.clear()
        for host, expires_mono in restored:
            _services().PUBLIC_SOURCE_COOLDOWNS[host] = expires_mono
    if expired:
        try:
            db.executemany("DELETE FROM app_meta WHERE key=?", expired)
        except _services().sqlite3.Error:
            pass


def _cooldown_public_source(url: str, seconds: float | None = None) -> float:
    key = _services()._public_source_key(url)
    duration = max(1.0, float(seconds or _services().PUBLIC_SOURCE_COOLDOWN_SECONDS))
    expires = _services().time.monotonic() + duration
    with _services().PUBLIC_SOURCE_STATE_LOCK:
        _services().PUBLIC_SOURCE_COOLDOWNS[key] = max(
            float(_services().PUBLIC_SOURCE_COOLDOWNS.get(key, 0.0) or 0.0), expires
        )
        effective_expires = _services().PUBLIC_SOURCE_COOLDOWNS[key]
    # Persist using wall-clock time because monotonic clocks are process-local.
    # A failed persistence write must never turn a recoverable network warning
    # into a failed search, so this is intentionally best effort.
    try:
        expires_wall = _services().time.time() + max(
            0.0, effective_expires - _services().time.monotonic()
        )
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "INSERT INTO app_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    _services().PUBLIC_SOURCE_COOLDOWN_META_PREFIX + key,
                    str(expires_wall),
                ),
            )
    except (OSError, _services().sqlite3.Error):
        pass
    return duration


def _public_retry_after(headers=None) -> float:
    """Honor a bounded Retry-After value for public source cooldowns."""
    try:
        value = float((headers or {}).get("Retry-After", ""))
    except (AttributeError, TypeError, ValueError):
        value = 0.0
    return min(max(value, _services().PUBLIC_SOURCE_COOLDOWN_SECONDS), 900.0)


@contextmanager
def model_usage_context(task: str, modality: str = "text"):
    """Attach a bounded task label to model calls made by this thread."""
    previous = getattr(_services().MODEL_USAGE_CONTEXT, "value", None)
    _services().MODEL_USAGE_CONTEXT.value = {
        "task": _services().clean_text(task, 40) or "analysis",
        "modality": _services().clean_text(modality, 24) or "text",
    }
    try:
        yield
    finally:
        if previous is None:
            try:
                del _services().MODEL_USAGE_CONTEXT.value
            except AttributeError:
                pass
        else:
            _services().MODEL_USAGE_CONTEXT.value = previous


def _model_usage_metadata(endpoint: str, payload: dict, attempts: int = 1) -> dict:
    context = getattr(_services().MODEL_USAGE_CONTEXT, "value", {})
    modality = _services().clean_text(context.get("modality", "text"), 24) or "text"
    if modality == "text" and _services().payload_text_chars(payload) < sum(
        len(str(part.get("image_url", {}).get("url", "")))
        for message in payload.get("messages", [])
        if isinstance(message, dict)
        for part in message.get("content", [])
        if isinstance(part, dict) and isinstance(part.get("image_url"), dict)
    ):
        modality = "multimodal"
    return {
        "task": _services().clean_text(context.get("task", "analysis"), 40)
        or "analysis",
        "model": _services().clean_text(payload.get("model", ""), 200),
        "endpoint_host": _services().endpoint_host(endpoint),
        "modality": modality,
        "input_chars": _services().payload_text_chars(payload),
        "retry_count": max(0, int(attempts) - 1),
        "started_perf": _services().time.perf_counter(),
    }


def record_model_usage(
    metadata: dict, result: dict | None = None, error: Exception | None = None
) -> None:
    """Persist token/cost/latency aggregates without retaining model content."""
    if not metadata:
        return
    usage = _services().response_usage(result or {})
    duration_ms = round(
        max(
            0.0,
            _services().time.perf_counter()
            - float(metadata.get("started_perf") or _services().time.perf_counter()),
        )
        * 1000,
        2,
    )
    status = "failed" if error else "success"
    now = _services().now_iso()
    day = now[:10]
    task = _services().clean_text(metadata.get("task", "analysis"), 40) or "analysis"
    model = _services().clean_text(metadata.get("model", ""), 200) or "unknown"
    host = _services().clean_text(metadata.get("endpoint_host", ""), 180)
    modality = _services().clean_text(metadata.get("modality", "text"), 24) or "text"
    input_tokens = usage["input_tokens"]
    output_tokens = usage["output_tokens"]
    total_tokens = usage["total_tokens"]
    cost = usage["cost_usd"]
    has_tokens = any(
        value is not None for value in (input_tokens, output_tokens, total_tokens)
    )
    error_type = type(error).__name__[:100] if error else ""
    try:
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                """INSERT INTO model_usage_daily(
                       day_utc,task,model,endpoint_host,modality,calls,failures,token_calls,
                       input_tokens,output_tokens,total_tokens,cost_calls,cost_usd,latency_sum_ms,latency_max_ms
                   ) VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(day_utc,task,model,endpoint_host,modality) DO UPDATE SET
                       calls=model_usage_daily.calls+1,
                       failures=model_usage_daily.failures+excluded.failures,
                       token_calls=model_usage_daily.token_calls+excluded.token_calls,
                       input_tokens=model_usage_daily.input_tokens+excluded.input_tokens,
                       output_tokens=model_usage_daily.output_tokens+excluded.output_tokens,
                       total_tokens=model_usage_daily.total_tokens+excluded.total_tokens,
                       cost_calls=model_usage_daily.cost_calls+excluded.cost_calls,
                       cost_usd=model_usage_daily.cost_usd+excluded.cost_usd,
                       latency_sum_ms=model_usage_daily.latency_sum_ms+excluded.latency_sum_ms,
                       latency_max_ms=max(model_usage_daily.latency_max_ms,excluded.latency_max_ms)""",
                (
                    day,
                    task,
                    model,
                    host,
                    modality,
                    int(bool(error)),
                    int(has_tokens),
                    input_tokens or 0,
                    output_tokens or 0,
                    total_tokens or 0,
                    int(cost is not None),
                    cost or 0.0,
                    duration_ms,
                    duration_ms,
                ),
            )
            event = db.execute(
                """INSERT INTO model_usage_events(
                       task,model,endpoint_host,modality,status,duration_ms,input_tokens,output_tokens,
                       total_tokens,cost_usd,retry_count,error_type,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task,
                    model,
                    host,
                    modality,
                    status,
                    duration_ms,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    cost,
                    max(0, int(metadata.get("retry_count", 0) or 0)),
                    error_type,
                    now,
                ),
            )
            # Detailed events support latency percentiles and recent failures;
            # the daily rollup remains durable for long-term trends.
            if int(event.lastrowid or 0) % 250 == 0:
                db.execute(
                    "DELETE FROM model_usage_events WHERE id NOT IN (SELECT id FROM model_usage_events ORDER BY id DESC LIMIT 10000)"
                )
    except (OSError, _services().sqlite3.Error, TypeError, ValueError):
        # Observability must never turn a successful inference into a failed job.
        return


def model_usage_summary(days: int = 30) -> dict:
    """Return bounded, privacy-safe provider usage and latency statistics."""
    days = max(1, min(365, int(days or 30)))
    cutoff = (
        _services().datetime.now(_services().timezone.utc).date()
        - _services().timedelta(days=days - 1)
    ).isoformat()
    empty = {
        "window_days": days,
        "calls": 0,
        "failures": 0,
        "error_rate_pct": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "token_reported_calls": 0,
        "reported_cost_usd": None,
        "cost_reported_calls": 0,
        "cost_coverage_pct": 0,
        "cost_basis": "provider_reported_only",
        "p50_ms": 0,
        "p95_ms": 0,
        "by_task": [],
        "recent_failures": [],
    }
    try:
        with _services().DB_LOCK, _services().db_session() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    """SELECT task,sum(calls) AS calls,sum(failures) AS failures,
                          sum(token_calls) AS token_calls,sum(input_tokens) AS input_tokens,
                          sum(output_tokens) AS output_tokens,sum(total_tokens) AS total_tokens,
                          sum(cost_calls) AS cost_calls,sum(cost_usd) AS cost_usd,
                          sum(latency_sum_ms) AS latency_sum_ms,max(latency_max_ms) AS latency_max_ms
                   FROM model_usage_daily WHERE day_utc>=? GROUP BY task ORDER BY calls DESC,task""",
                    (cutoff,),
                ).fetchall()
            ]
            samples = [
                float(row[0] or 0)
                for row in db.execute(
                    "SELECT duration_ms FROM model_usage_events WHERE created_at>=? ORDER BY id DESC LIMIT 10000",
                    (cutoff + "T00:00:00",),
                ).fetchall()
            ]
            failures = [
                dict(row)
                for row in db.execute(
                    "SELECT task,model,error_type,created_at FROM model_usage_events WHERE status='failed' AND created_at>=? ORDER BY id DESC LIMIT 8",
                    (cutoff + "T00:00:00",),
                ).fetchall()
            ]
    except (OSError, _services().sqlite3.Error):
        return empty
    total_calls = sum(int(row["calls"] or 0) for row in rows)
    total_failures = sum(int(row["failures"] or 0) for row in rows)
    token_calls = sum(int(row["token_calls"] or 0) for row in rows)
    cost_calls = sum(int(row["cost_calls"] or 0) for row in rows)
    observed_cost = sum(float(row["cost_usd"] or 0) for row in rows)
    by_task = [
        {
            "task": row["task"],
            "calls": int(row["calls"] or 0),
            "failures": int(row["failures"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "reported_cost_usd": (
                round(float(row["cost_usd"] or 0), 8) if row["cost_calls"] else None
            ),
            "cost_reported_calls": int(row["cost_calls"] or 0),
            "mean_latency_ms": round(
                float(row["latency_sum_ms"] or 0) / max(1, int(row["calls"] or 0)), 2
            ),
            "max_latency_ms": round(float(row["latency_max_ms"] or 0), 2),
        }
        for row in rows
    ]
    return {
        "window_days": days,
        "calls": total_calls,
        "failures": total_failures,
        "error_rate_pct": (
            round(total_failures * 100 / total_calls, 2) if total_calls else 0
        ),
        "input_tokens": sum(int(row["input_tokens"] or 0) for row in rows),
        "output_tokens": sum(int(row["output_tokens"] or 0) for row in rows),
        "total_tokens": sum(int(row["total_tokens"] or 0) for row in rows),
        "token_reported_calls": token_calls,
        "token_coverage_pct": (
            round(token_calls * 100 / total_calls, 2) if total_calls else 0
        ),
        "reported_cost_usd": round(observed_cost, 8) if cost_calls else None,
        "cost_reported_calls": cost_calls,
        "cost_coverage_pct": (
            round(cost_calls * 100 / total_calls, 2) if total_calls else 0
        ),
        "cost_basis": "provider_reported_only",
        "p50_ms": _services()._percentile(samples, 50),
        "p95_ms": _services()._percentile(samples, 95),
        "by_task": by_task,
        "recent_failures": failures,
    }


def open_compatible_model_request(
    endpoint: str,
    payload: dict,
    api_key: str,
    timeout: int,
    accept: str = "application/json",
):
    """Retry bounded gateway and transport failures, then rejected optional fields.

    ``urlopen`` reports a dropped relay connection as ``URLError`` or
    ``TimeoutError`` rather than ``HTTPError``.  Treat those as transient only
    for a small number of attempts; authentication, validation, and endpoint
    errors still return immediately with their original diagnostics.
    """
    transient_retries = 0
    attempts = 0
    started_perf = _services().time.perf_counter()
    for _attempt in range(8):
        attempts += 1
        headers = {
            "Content-Type": "application/json",
            "Accept": accept,
            "User-Agent": _services().USER_AGENT,
        }
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        request = _services().Request(
            endpoint,
            data=_services().json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            response = _services().urlopen(request, timeout=timeout)
            metadata = _services()._model_usage_metadata(endpoint, payload, attempts)
            metadata["started_perf"] = started_perf
            try:
                response._litloom_model_usage = metadata
            except (AttributeError, TypeError):
                pass
            return response
        except _services().HTTPError as exc:
            if (
                exc.code in _services().MODEL_TRANSIENT_HTTP_CODES
                and transient_retries < _services().MODEL_TRANSIENT_RETRIES
            ):
                delay = _services()._model_retry_delay(transient_retries, exc.headers)
                transient_retries += 1
                _services().time.sleep(delay)
                continue
            detail = _services().model_error_detail(exc, api_key)
            folded = detail.casefold()
            generic_optional_error = any(
                term in folded
                for term in ("unknown parameter", "extra fields", "unrecognized")
            )
            if exc.code == 400 and "store" in folded and "store" in payload:
                payload.pop("store", None)
                continue
            if (
                exc.code == 400
                and (
                    "response_format" in folded
                    or "json_object" in folded
                    or "text.format" in folded
                    or "format" in folded
                )
                and ("response_format" in payload or "text" in payload)
            ):
                payload.pop("response_format", None)
                # `text` is a Responses-only output-format field. Do not
                # remove unrelated text content from a Chat payload.
                if isinstance(payload.get("text"), dict) and isinstance(
                    payload["text"].get("format"), dict
                ):
                    payload.pop("text", None)
                continue
            if (
                exc.code == 400
                and ("reasoning" in folded or generic_optional_error)
                and ("reasoning" in payload or "reasoning_effort" in payload)
            ):
                payload.pop("reasoning", None)
                payload.pop("reasoning_effort", None)
                continue
            if (
                exc.code == 400
                and payload.get("stream")
                and ("stream" in folded or generic_optional_error)
            ):
                payload["stream"] = False
                continue
            if (
                exc.code == 400
                and payload.get("tool_choice") == "required"
                and (
                    "tool_choice" in folded
                    or "required" in folded
                    or generic_optional_error
                )
            ):
                # A few older OpenAI-compatible relays understand tools but
                # only accept ``auto``. Fall back once instead of treating a
                # valid provider as unavailable.
                payload["tool_choice"] = "auto"
                continue
            if (
                exc.code == 400
                and (
                    "max_output_tokens" in folded
                    or "max_tokens" in folded
                    or generic_optional_error
                )
                and ("max_output_tokens" in payload or "max_tokens" in payload)
            ):
                payload.pop("max_output_tokens", None)
                payload.pop("max_tokens", None)
                continue
            try:
                exc._litloom_error_detail = (
                    detail.split("：", 1)[1] if "：" in detail else detail
                )
            except Exception:
                pass
            metadata = _services()._model_usage_metadata(endpoint, payload, attempts)
            metadata["started_perf"] = started_perf
            _services().record_model_usage(metadata, error=exc)
            raise
        except (_services().URLError, TimeoutError, ConnectionError, OSError):
            # DNS/connection resets and read timeouts are usually relay-side
            # transients.  Do not retry arbitrary ValueError/JSON errors here.
            if transient_retries >= _services().MODEL_TRANSIENT_RETRIES:
                metadata = _services()._model_usage_metadata(
                    endpoint, payload, attempts
                )
                metadata["started_perf"] = started_perf
                _services().record_model_usage(
                    metadata, error=TimeoutError("provider transport failed")
                )
                raise
            delay = _services()._model_retry_delay(transient_retries)
            transient_retries += 1
            _services().time.sleep(delay)
            continue
    error = ValueError("模型服务多次拒绝兼容参数，已停止重试")
    metadata = _services()._model_usage_metadata(endpoint, payload, attempts)
    metadata["started_perf"] = started_perf
    _services().record_model_usage(metadata, error=error)
    raise error


def agent_discovery_plan(
    query: str, sources: list[str], options: dict | None = None
) -> dict:
    """Optionally add model-proposed terminology to the deterministic plan.

    Only the user's query and extracted facets are sent.  The model cannot
    write to the library; its output is treated as candidate search wording
    and is bounded, parsed and shown as a separate lane.
    """
    raw = _services().clean_text(query, 1000)
    if not raw:
        raise ValueError("需要检索词")
    active_sources = [
        source
        for source in sources
        if source in _services().SOURCE_NAMES and source in _services().SOURCE_SEARCH
    ]
    plan = _services().search_query_plan(raw, active_sources, options or {})
    search_depth = (
        _services()
        .normalize_search_options(options or {})
        .get("search_depth", "balanced")
    )
    # The model is only needed for semantic vocabulary expansion. Keep its
    # output proportional to the connector budget: ordinary/focused searches
    # should not spend tokens producing variants that will never be queried.
    ai_variant_budget = {"focused": 2, "balanced": 2, "deep": 5}.get(search_depth, 2)
    settings = _services().resolve_model_route(_services().model_settings(), "fast")
    api_key = _services().model_api_key()
    if not api_key or not settings["enabled"]:
        plan["ai_expansion"] = {
            "enabled": False,
            "reason": "未启用模型，已使用本地规则检索计划",
        }
        return plan
    prompt = (
        "你是学术检索词规划器。把研究问题压缩成英文数据库检索词，优先保留研究对象和核心任务，方法词只作约束；"
        "不要把具体任务泛化成整个学科，不要编造论文、作者、DOI或事实。只输出 JSON 对象，必须包含 "
        '"synonyms"、"adjacent_terms"、"queries" 三个字符串数组。'
        f"总共最多提出 {ai_variant_budget} 个候选短语；每个短语 3-8 个英文单词，不写句子或解释。"
        '格式示例：{"synonyms":["food volume estimation"],"adjacent_terms":[],"queries":["food portion size measurement"]}。'
    )
    user = {
        "query": raw,
        "facets": plan.get("facets", {}),
        "existing_terms": plan.get("expanded_concept_terms", [])[:12],
    }

    def normalize_proposal(proposal: dict) -> tuple[list[str], list[str], list[str]]:
        normalized = []
        for key in ("synonyms", "adjacent_terms", "queries"):
            if key not in proposal:
                raise ValueError(f"检索规划 JSON 缺少 {key} 字段")
            values = proposal[key]
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                raise ValueError(f"检索规划 JSON 的 {key} 必须是字符串数组")
            terms = []
            for value in values:
                if not isinstance(value, str):
                    continue
                candidate = _services().clean_text(value, 240)
                if candidate and candidate.casefold() not in {
                    item.casefold() for item in terms
                }:
                    terms.append(candidate)
            normalized.append(terms)
        return tuple(normalized)

    try:
        proposal = _services().model_json_request_for_task(
            prompt, user, "fast", timeout=60, max_output_tokens=1200
        )
        try:
            synonyms, adjacent_terms, queries = normalize_proposal(proposal)
        except ValueError as schema_error:
            repair_prompt = (
                prompt
                + "\n上一次返回的 JSON 字段格式不符合要求："
                + str(schema_error)
                + "。请修正并且只输出包含全部三个必需键的 JSON 对象。"
            )
            proposal = _services().model_json_request_for_task(
                repair_prompt, user, "fast", timeout=60, max_output_tokens=1800
            )
            synonyms, adjacent_terms, queries = normalize_proposal(proposal)
    except (
        _services().HTTPError,
        _services().URLError,
        TimeoutError,
        OSError,
        _services().json.JSONDecodeError,
        ValueError,
    ) as exc:
        message = str(exc)
        # A malformed model answer must not block the deterministic search
        # plan. Keep the reason visible to the UI, but let the user continue
        # searching and decide whether to fix the provider configuration. The
        # same rule applies to transport/provider failures: the planning
        # endpoint is an optional AI expansion, never the search itself.
        fallback_reason = _services().clean_text(message, 240) or "模型未返回可解析内容"
        plan["ai_expansion"] = {
            "enabled": False,
            "model": settings.get("model", ""),
            "added": 0,
            "requires_review": False,
            "fallback": True,
            "reason": f"模型扩展未生成有效 JSON（{fallback_reason}），已回退本地规则检索计划",
        }
        return plan

    existing = {
        item.get("query", "").casefold() for item in plan.get("query_variants", [])
    }
    added = []
    for kind, values in (
        ("AI同义词", synonyms),
        ("AI相邻术语", adjacent_terms),
        ("AI检索表达", queries),
    ):
        for value in values:
            candidate = value
            if (
                not candidate
                or candidate.casefold() in existing
                or len(added) >= ai_variant_budget
            ):
                continue
            existing.add(candidate.casefold())
            lane = "ai-adjacent" if kind == "AI相邻术语" else "ai"
            added.append(
                {
                    "id": f"ai-{len(added) + 1}",
                    "query": candidate,
                    "kind": kind,
                    "reason": "模型根据当前研究问题提出，需人工确认后用于召回",
                    "lane": lane,
                }
            )
    plan["query_variants"] = [*plan.get("query_variants", []), *added]
    plan["expansion_count"] = max(0, len(plan["query_variants"]) - 1)
    if added:
        plan["coverage_layers"] = [
            *plan.get("coverage_layers", []),
            "模型语义扩展（待确认）",
        ]
    plan["ai_expansion"] = {
        "enabled": True,
        "model": settings["model"],
        "added": len(added),
        "budget": ai_variant_budget,
        "search_depth": search_depth,
        "requires_review": True,
    }
    return plan


__all__ = [
    "_model_request_slot",
    "_pdf_render_slot",
    "_with_model_request_budget",
    "cached_derived",
    "invalidate_derived_cache",
    "database_change_token",
    "now_iso",
    "_percentile",
    "record_runtime_request",
    "runtime_metrics_summary",
    "local_model_key_path",
    "read_local_model_key",
    "write_local_model_key",
    "clear_local_model_key",
    "model_api_key_details",
    "model_api_key",
    "model_provider_presets",
    "model_endpoint_warning",
    "model_settings",
    "save_model_settings",
    "read_model_first_token",
    "test_model_connection",
    "model_endpoint",
    "add_model_reasoning",
    "add_json_output_format",
    "model_base_url",
    "model_response_content",
    "model_error_detail",
    "_model_retry_delay",
    "PublicSourceCooldownError",
    "fast_model_search_source_budget",
    "_public_source_request_policy",
    "_public_source_key",
    "_public_source_request_lock",
    "_public_source_cooldown_remaining",
    "_load_persisted_public_cooldowns",
    "_cooldown_public_source",
    "_public_retry_after",
    "model_usage_context",
    "_model_usage_metadata",
    "record_model_usage",
    "model_usage_summary",
    "open_compatible_model_request",
    "agent_discovery_plan",
]
