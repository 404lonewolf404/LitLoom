from __future__ import annotations

import hashlib
import ipaddress
import base64
import contextvars
import inspect
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import tempfile
import unicodedata
import uuid
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.parser import BytesFeedParser, BytesParser
from email.policy import default
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """Propagate request/workspace context into bounded background workers."""

    def submit(self, fn, /, *args, **kwargs):
        context = contextvars.copy_context()
        return super().submit(context.run, fn, *args, **kwargs)


# All in-process pools use this alias. It keeps tenant scope and request
# tracing attached to PDF, model, connector, and Agent work without changing
# every worker signature.
ThreadPoolExecutor = ContextThreadPoolExecutor

from literature_desk import __version__
from literature_desk.domain import PAPER_STATUSES, QUERY_DECISIONS, RECOMMENDATION_DECISIONS, SOURCE_NAMES
from literature_desk.embeddings import clean_embedding_text, cosine_similarity, reciprocal_rank_fusion, split_text, validate_embedding_response
from literature_desk.model_router import resolve_model_route
from literature_desk.model_telemetry import endpoint_host, payload_text_chars, response_usage
from literature_desk.release import release_snapshot
from literature_desk.config import RuntimeConfig, configuration_snapshot, load_env_file
from literature_desk.errors import error_payload, status_error_code
from literature_desk.security import (
    BOOTSTRAP_TOKEN_BYTES,
    constant_time_token_match,
    hash_password,
    new_token,
    token_digest,
    validate_password,
    validate_username,
    verify_password,
)
from literature_desk.postgres import (
    connect as postgres_connect,
    apply_migrations as postgres_apply_migrations,
    database_label as postgres_database_label,
    dump_database as postgres_dump_database,
    ensure_local_server as postgres_ensure_local_server,
    integrity_check as postgres_integrity_check,
    migrate_source as postgres_migrate_source,
    restore_dump as postgres_restore_dump,
    table_exists as postgres_table_exists,
    validate_dump as postgres_validate_dump,
)


ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Load a small, dependency-free .env file without overriding the OS env."""
    configured_path = os.environ.get("LITERATURE_ENV_FILE", "").strip()
    path = Path(configured_path).expanduser() if configured_path else ROOT / ".env"
    load_env_file(path, os.environ)


_load_dotenv()
STATIC = ROOT / "static"
if not STATIC.is_dir():
    # Wheel installs place data-files under the interpreter prefix while
    # editable/source installs keep them beside app.py.
    STATIC = Path(sys.prefix) / "static"
RUNTIME_CONFIG = RuntimeConfig.from_env(os.environ, root=ROOT)
DATA_DIR = RUNTIME_CONFIG.data_dir
FILES_DIR = DATA_DIR / "files"
VISUALS_DIR = DATA_DIR / "visual-pages"
BACKUP_DIR = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "literature.sqlite3"
POSTGRES_MIGRATION_RESULT = None
WORKSPACE_DB_DIR = DATA_DIR / "workspaces"
HOST = RUNTIME_CONFIG.host
PORT = RUNTIME_CONFIG.port
if not ipaddress.ip_address(HOST).is_loopback:
    raise RuntimeError("LitLoom 仅允许绑定本机回环地址；请将 LITERATURE_HOST 设为 127.0.0.1")
APP_VERSION = __version__
# Discovery triage and its cross-run feedback memory are additive tables.
# ``init_db`` creates them idempotently for existing local workspaces.
DB_SCHEMA_VERSION = 19
USER_AGENT = f"LitLoom/{APP_VERSION} (local research tool; mailto:local@example.invalid)"
# Presets only fill the editable form. Runtime requests always use the values
# saved by the user, so a preset never pins the application to a vendor.
MODEL_PROVIDER_PRESETS = (
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "website_url": "https://www.deepseek.com",
        "base_url": "https://api.deepseek.com",
        "api_url": "https://api.deepseek.com/chat/completions",
        "protocol": "chat_completions",
        "models": ["deepseek-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-flash",
        "notes": "DeepSeek OpenAI 兼容接口；请使用你自己的 API Key。",
    },
)
DB_LOCK = threading.RLock()
DERIVED_CACHE_LOCK = threading.RLock()
DERIVED_CACHE = {}
DERIVED_CACHE_GENERATION = 0
DATABASE_TOKEN_CACHE = {"value": "", "at": 0.0}
PUBLIC_SOURCE_STATE_LOCK = threading.RLock()
PUBLIC_SOURCE_COOLDOWNS = {}
# Public-index cooldowns survive a process restart.  Only a host name and an
# absolute expiry timestamp are stored in ``app_meta``; query strings,
# responses, and credentials never enter this table.
PUBLIC_SOURCE_COOLDOWN_META_PREFIX = "public_source_cooldown:"
# A cooldown check by itself is not enough when several search lanes start at
# the same time: they can all observe an open circuit and then hit OpenAlex
# before the first 429 has a chance to record the cooldown.  Keep one small
# request gate per public host.  Different hosts (arXiv, Crossref, OpenAlex,
# etc.) remain concurrent, while requests to a rate-limited host are serialized
# and re-check the cooldown after waiting.
PUBLIC_SOURCE_REQUEST_LOCKS_LOCK = threading.RLock()
PUBLIC_SOURCE_REQUEST_LOCKS = {}
# Model-led discovery fans out over several public indexes. Keep a separate
# thread-local fast budget for that path so one stalled index cannot hold the
# model conversation for a full connector retry window. Normal saved-query
# synchronization continues to use the more forgiving public-source policy.
MODEL_SEARCH_FAST_SOURCE = threading.local()
TAVILY_VERIFY_CACHE = {}
TAVILY_VERIFY_CACHE_LOCK = threading.RLock()
OPENALEX_RECOMMENDATION_CACHE = {}
OPENALEX_RECOMMENDATION_CACHE_LOCK = threading.RLock()
OPENALEX_RECOMMENDATION_EXECUTOR = None
OPENALEX_RECOMMENDATION_EXECUTOR_LOCK = threading.Lock()
OPENALEX_RECOMMENDATION_TTL_SECONDS = 6 * 60 * 60
OPENALEX_RECOMMENDATION_ERROR_BACKOFF_SECONDS = 5 * 60
SEMANTIC_RECOMMENDATION_MAX_SEEDS = 3
SEMANTIC_RECOMMENDATION_MAX_RESULTS = 50
BACKUP_LOCK = threading.Lock()
AUTO_BACKUP_RUN_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
# The process is deliberately local-first, but it still has several durable
# background executors.  Keep shutdown state separate from the database state
# so a request arriving during a controlled restart cannot leave a queued row
# that has no worker behind it.
RUNTIME_SHUTDOWN_MESSAGE = "服务正在停止；任务已保存，将在下次启动后自动恢复"
RUNTIME_MODEL_KEY = ""
PROCESS_STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
AUTH_ACTOR = contextvars.ContextVar("litloom_auth_actor", default=None)
AUTH_SESSION_TTL_DAYS = 7
AUTH_SESSION_MAX_AGE_DAYS = 30
AUTH_LOGIN_WINDOW_SECONDS = 600
AUTH_LOGIN_MAX_FAILURES = 8
# ``init_db`` fills this only for jobs it interrupted during the current
# process startup. Older historical interruptions are deliberately left alone
# and can be resumed explicitly from the UI/API.
STARTUP_INTERRUPTED_SYNC_IDS = []
STARTUP_INTERRUPTED_AGENT_IDS = []
STARTUP_INTERRUPTED_BY_WORKSPACE = {}
SCHEDULER_STARTED_AT = ""
SCHEDULER_LAST_TICK = ""
AI_JOB_EXECUTOR = None
AI_JOB_EXECUTOR_LOCK = threading.Lock()
METADATA_DRAFT_EXECUTOR = None
METADATA_DRAFT_EXECUTOR_LOCK = threading.Lock()
SEMANTIC_INDEX_EXECUTOR = None
SEMANTIC_INDEX_EXECUTOR_LOCK = threading.Lock()
DISCOVERY_SCREENING_LOCK = threading.Lock()
DISCOVERY_SCREENING_RUNS = set()
MODEL_REQUEST_SEMAPHORE = None
MODEL_REQUEST_SEMAPHORE_LOCK = threading.Lock()
MODEL_USAGE_CONTEXT = threading.local()
PDF_RENDER_SEMAPHORE = None
PDF_RENDER_SEMAPHORE_LOCK = threading.Lock()
AGENT_RUN_EXECUTOR = None
AGENT_RUN_EXECUTOR_LOCK = threading.Lock()
AGENT_TOOL_EXECUTOR = None
AGENT_TOOL_EXECUTOR_LOCK = threading.Lock()
AGENT_ACTIVE_RUNS = set()
AGENT_ACTIVE_RUNS_LOCK = threading.Lock()
PDF_INGEST_EXECUTOR = None
PDF_INGEST_EXECUTOR_LOCK = threading.Lock()
BACKGROUND_THREADS = set()
BACKGROUND_THREADS_LOCK = threading.Lock()
try:
    AUTO_BACKUP_INTERVAL_HOURS = max(0.0, float(os.environ.get("LITERATURE_AUTO_BACKUP_HOURS", "24")))
except (TypeError, ValueError):
    AUTO_BACKUP_INTERVAL_HOURS = 24.0
try:
    BACKUP_RETENTION = max(1, min(365, int(os.environ.get("LITERATURE_BACKUP_RETENTION", "14"))))
except (TypeError, ValueError):
    BACKUP_RETENTION = 14
try:
    FULLTEXT_CONCURRENCY = max(1, min(4, int(os.environ.get("LIT_AGENT_FULLTEXT_CONCURRENCY", "4"))))
except (TypeError, ValueError):
    FULLTEXT_CONCURRENCY = 4
try:
    # A local desktop should remain responsive while a relay is generating.
    # This is a global budget shared by chunk analysis and other model calls.
    MODEL_CONCURRENCY = max(1, min(4, int(os.environ.get("LIT_AGENT_MODEL_CONCURRENCY", "4"))))
except (TypeError, ValueError):
    MODEL_CONCURRENCY = 4
try:
    # Four items keeps each structured screening response small enough for
    # reasoning-capable relays while the screening pool still runs groups in
    # parallel. Larger batches were prone to omitted rows and compensating
    # retries even when the provider eventually had enough context.
    DISCOVERY_SCREEN_GROUP_SIZE = max(4, min(10, int(os.environ.get("LIT_DISCOVERY_SCREEN_GROUP_SIZE", "4"))))
except (TypeError, ValueError):
    DISCOVERY_SCREEN_GROUP_SIZE = 8
try:
    DISCOVERY_SCREEN_RETRIES = max(0, min(3, int(os.environ.get("LIT_DISCOVERY_SCREEN_RETRIES", "2"))))
except (TypeError, ValueError):
    DISCOVERY_SCREEN_RETRIES = 2
try:
    # Independent paper analyses may run together.  The shared model semaphore
    # still caps all provider requests, while the PDF/render budgets protect a
    # desktop from unbounded memory pressure.  Two workers keeps a reading
    # card or prerequisite guide from waiting behind a deep-read job.
    AI_JOB_CONCURRENCY = max(1, min(2, int(os.environ.get("LIT_AGENT_AI_JOB_CONCURRENCY", "2"))))
except (TypeError, ValueError):
    AI_JOB_CONCURRENCY = 2
try:
    # A ready DAG wave may include independent local and model-backed work.
    # Keep the fan-out bounded so concurrent user runs cannot saturate a desktop.
    AGENT_STEP_CONCURRENCY = max(1, min(4, int(os.environ.get("LIT_AGENT_STEP_CONCURRENCY", "3"))))
except (TypeError, ValueError):
    AGENT_STEP_CONCURRENCY = 3
try:
    MODEL_SLOT_TIMEOUT = max(5, min(600, int(os.environ.get("LIT_AGENT_MODEL_SLOT_TIMEOUT", "30"))))
except (TypeError, ValueError):
    MODEL_SLOT_TIMEOUT = 30
try:
    PDF_RENDER_CONCURRENCY = max(1, min(2, int(os.environ.get("LIT_AGENT_PDF_RENDER_CONCURRENCY", "1"))))
except (TypeError, ValueError):
    PDF_RENDER_CONCURRENCY = 1
try:
    # Downloads and OCR are independent per paper/page. Keep their pools
    # separate from model calls so a slow provider cannot starve local work.
    PDF_DOWNLOAD_CONCURRENCY = max(1, min(4, int(os.environ.get("LIT_AGENT_PDF_DOWNLOAD_CONCURRENCY", "3"))))
except (TypeError, ValueError):
    PDF_DOWNLOAD_CONCURRENCY = 3
try:
    PDF_IMPORT_CONCURRENCY = max(1, min(3, int(os.environ.get("LIT_AGENT_PDF_IMPORT_CONCURRENCY", "2"))))
except (TypeError, ValueError):
    PDF_IMPORT_CONCURRENCY = 2
try:
    OCR_CONCURRENCY = max(1, min(2, int(os.environ.get("LIT_AGENT_OCR_CONCURRENCY", "2"))))
except (TypeError, ValueError):
    OCR_CONCURRENCY = 2
try:
    # Deep reads keep the full text as the evidence backbone. A moderate
    # default chunk lets independent sections run concurrently on a desktop;
    # very large single chunks make a short paper wait on one slow reasoning
    # request and prevent the visual/text passes from overlapping. The
    # environment override remains available for relays with a larger or
    # smaller context window.
    FULLTEXT_CHUNK_CHARS = max(9000, min(24000, int(os.environ.get("LIT_AGENT_FULLTEXT_CHUNK_CHARS", "14000"))))
except (TypeError, ValueError):
    FULLTEXT_CHUNK_CHARS = 22000
try:
    # A structured reading card is the fast first pass. It reads all page
    # text in larger chunks; the separate deep-read workflow handles visuals.
    READING_CARD_CHUNK_CHARS = max(14000, min(120000, int(os.environ.get("LIT_AGENT_READING_CARD_CHUNK_CHARS", "110000"))))
except (TypeError, ValueError):
    READING_CARD_CHUNK_CHARS = 110000
# Keep the quick card responsive by sending only high-signal pages. Full text
# remains available to the deep-read workflow and to local evidence checks.
try:
    READING_CARD_CONTEXT_CHARS = max(18000, min(80000, int(os.environ.get("LIT_AGENT_READING_CARD_CONTEXT_CHARS", "36000"))))
except (TypeError, ValueError):
    READING_CARD_CONTEXT_CHARS = 36000
# Fast reading cards are intentionally text-only; visual analysis belongs to deep read.
READING_CARD_VISUAL_PAGES_PER_CHUNK = 0
try:
    DEEP_READ_VISUAL_PAGES_PER_CHUNK = max(1, min(4, int(os.environ.get("LIT_AGENT_DEEP_READ_VISUAL_PAGES_PER_CHUNK", "2"))))
except (TypeError, ValueError):
    DEEP_READ_VISUAL_PAGES_PER_CHUNK = 2
try:
    # Long deep reads use a separate, page-sized visual pass.  Keeping the
    # threshold explicit preserves the compact one-request path for short
    # papers while avoiding repeated multi-page text in image requests.
    DEEP_READ_SEPARATE_VISUAL_MIN_CHARS = max(
        12000, min(120000, int(os.environ.get("LIT_AGENT_DEEP_READ_SEPARATE_VISUAL_MIN_CHARS", "30000")))
    )
except (TypeError, ValueError):
    DEEP_READ_SEPARATE_VISUAL_MIN_CHARS = 30000
try:
    # A visual recheck is only scheduled when a rendered page has a strong
    # figure/table/equation cue but the first multimodal pass returned no
    # observation for that page. Keep this bounded so a noisy PDF cannot turn
    # the recovery path into another full-document run.
    # A separate visual page pass already covers the highest-signal pages.
    # Keep one bounded recovery page by default so a provider that returns
    # empty visual observations does not double the multimodal work.
    DEEP_READ_VISUAL_RECHECK_MAX_PAGES = max(0, min(8, int(os.environ.get("LIT_AGENT_DEEP_READ_VISUAL_RECHECK_MAX_PAGES", "1"))))
except (TypeError, ValueError):
    DEEP_READ_VISUAL_RECHECK_MAX_PAGES = 4
READING_CARD_PIPELINE_VERSION = "fulltext-reading-card-single-pass-v12"
DEEP_READ_PIPELINE_VERSION = "fulltext-analysis-v21"
PREREADING_GUIDE_PIPELINE_VERSION = "paper-prereading-guide-v6"
try:
    # Keep model responses bounded so a relay/proxy cannot hold a request for
    # minutes while producing an unnecessarily large JSON answer.  Providers
    # may override this through the environment; zero disables the hint.
    # Zero omits the optional provider output cap.  This prevents an
    # application-side online limit from truncating long structured answers;
    # deployments can still set a positive value as an explicit safeguard.
    MODEL_MAX_OUTPUT_TOKENS = max(0, min(32000, int(os.environ.get("LIT_AGENT_MAX_OUTPUT_TOKENS", "0"))))
except (TypeError, ValueError):
    MODEL_MAX_OUTPUT_TOKENS = 0
MODEL_STREAMING = os.environ.get("LIT_AGENT_STREAM", "1").strip().lower() not in {"0", "false", "no", "off"}
# A model request can legitimately take longer than the browser polling
# interval, especially for a multimodal relay.  Emit a lightweight durable
# progress heartbeat while that request is in flight so the UI does not look
# frozen.  This never creates another model request and is intentionally
# bounded to avoid turning status updates into database contention.
try:
    MODEL_PROGRESS_HEARTBEAT_SECONDS = max(
        2.0, min(30.0, float(os.environ.get("LIT_AGENT_PROGRESS_HEARTBEAT", "5")))
    )
except (TypeError, ValueError):
    MODEL_PROGRESS_HEARTBEAT_SECONDS = 5.0
# Keep retries short and bounded.  A relay can transiently reset a connection
# even when the model itself is healthy; retrying only these transport/gateway
# failures avoids turning a bad key or malformed payload into a long wait.
try:
    MODEL_TRANSIENT_RETRIES = max(0, min(4, int(os.environ.get("LIT_AGENT_TRANSIENT_RETRIES", "2"))))
except (TypeError, ValueError):
    MODEL_TRANSIENT_RETRIES = 2
try:
    MODEL_RETRY_BASE_DELAY = max(0.05, min(2.0, float(os.environ.get("LIT_AGENT_RETRY_BASE_DELAY", "0.35"))))
except (TypeError, ValueError):
    MODEL_RETRY_BASE_DELAY = 0.35
try:
    MODEL_RETRY_MAX_DELAY = max(MODEL_RETRY_BASE_DELAY, min(8.0, float(os.environ.get("LIT_AGENT_RETRY_MAX_DELAY", "2.0"))))
except (TypeError, ValueError):
    MODEL_RETRY_MAX_DELAY = 2.0
# Public scholarly indexes have very different rate limits from the model
# gateway. A connector that has just returned 429 should be cooled down for
# the remainder of the current burst instead of being queried once per
# expansion lane. This keeps one unavailable source from holding up a search.
try:
    PUBLIC_SOURCE_TIMEOUT = max(5, min(30, int(os.environ.get("LIT_AGENT_SOURCE_TIMEOUT", "12"))))
except (TypeError, ValueError):
    PUBLIC_SOURCE_TIMEOUT = 12
try:
    PUBLIC_SOURCE_RETRIES = max(0, min(2, int(os.environ.get("LIT_AGENT_SOURCE_RETRIES", "1"))))
except (TypeError, ValueError):
    PUBLIC_SOURCE_RETRIES = 1
try:
    PUBLIC_SOURCE_COOLDOWN_SECONDS = max(10, min(900, int(os.environ.get("LIT_AGENT_SOURCE_COOLDOWN", "45"))))
except (TypeError, ValueError):
    PUBLIC_SOURCE_COOLDOWN_SECONDS = 45
try:
    TAVILY_TIMEOUT = max(5, min(30, int(os.environ.get("LIT_AGENT_TAVILY_TIMEOUT", "12"))))
except (TypeError, ValueError):
    TAVILY_TIMEOUT = 12
try:
    # AI-led discovery already queries several sources in parallel. A source
    # that does not answer quickly should be recorded as unavailable so the
    # model can use the remaining sources instead of waiting through the
    # normal synchronization retry window.
    MODEL_SEARCH_SOURCE_TIMEOUT = max(3, min(20, int(os.environ.get("LIT_AGENT_MODEL_SEARCH_SOURCE_TIMEOUT", "8"))))
except (TypeError, ValueError):
    MODEL_SEARCH_SOURCE_TIMEOUT = 8
try:
    # Continuation pages are a best-effort refill for repeat-heavy searches.
    # Give them a shorter budget than the first page so stale provider pages
    # cannot turn a three-angle search into a minute-long wait.
    MODEL_SEARCH_CONTINUATION_TIMEOUT = max(
        2, min(MODEL_SEARCH_SOURCE_TIMEOUT, int(os.environ.get("LIT_AGENT_MODEL_SEARCH_CONTINUATION_TIMEOUT", "4")))
    )
except (TypeError, ValueError):
    MODEL_SEARCH_CONTINUATION_TIMEOUT = min(MODEL_SEARCH_SOURCE_TIMEOUT, 4)
try:
    # Only title-only web hits need an extra academic-index lookup. DOI and
    # arXiv hits remain cheap and are always verified; the cap prevents a
    # Tavily page of generic web results from turning into dozens of serial
    # Crossref/OpenAlex requests.
    TAVILY_VERIFY_MAX_TITLE_ONLY = max(1, min(8, int(os.environ.get("LIT_AGENT_TAVILY_VERIFY_MAX", "3"))))
except (TypeError, ValueError):
    TAVILY_VERIFY_MAX_TITLE_ONLY = 3
MODEL_REASONING_EFFORT = os.environ.get("LIT_AGENT_REASONING_EFFORT", "low").strip().lower()
if MODEL_REASONING_EFFORT not in {"", "minimal", "low", "medium", "high"}:
    MODEL_REASONING_EFFORT = "low"
# Search planning and final candidate selection only need short structured
# decisions. Keep their reasoning budget independent from paper analysis so a
# relay cannot spend most of a search response on hidden chain-of-thought.
MODEL_SEARCH_REASONING_EFFORT = os.environ.get("LIT_AGENT_SEARCH_REASONING_EFFORT", "minimal").strip().lower()
if MODEL_SEARCH_REASONING_EFFORT not in {"", "minimal", "low", "medium", "high"}:
    MODEL_SEARCH_REASONING_EFFORT = "minimal"
# Page/chunk extraction is a constrained evidence pass. It benefits from a
# short direct response and does not need a second reasoning trace; leaving
# this unset lets some relays silently choose their slow/high default. Use the
# provider's lowest explicit reasoning tier by default, while keeping the
# final synthesis on the global ``low`` setting. This is a latency control,
# not an output-token cap, so structured answers are still allowed to finish.
MODEL_ANALYSIS_REASONING_EFFORT = os.environ.get("LIT_AGENT_ANALYSIS_REASONING_EFFORT", "minimal").strip().lower()
if MODEL_ANALYSIS_REASONING_EFFORT not in {"", "minimal", "low", "medium", "high"}:
    MODEL_ANALYSIS_REASONING_EFFORT = ""
MODEL_PROBE_REASONING = os.environ.get("LIT_AGENT_PROBE_REASONING", "low").strip().lower()
if MODEL_PROBE_REASONING not in {"", "minimal", "low", "medium", "high"}:
    MODEL_PROBE_REASONING = "low"
try:
    # A connection probe should fail fast and must not inherit the much longer
    # full-document inference timeout.
    MODEL_PROBE_TIMEOUT = max(5, min(120, int(os.environ.get("LIT_AGENT_PROBE_TIMEOUT", "20"))))
except (TypeError, ValueError):
    MODEL_PROBE_TIMEOUT = 20
try:
    MODEL_CHUNK_MAX_OUTPUT_TOKENS = max(0, min(8000, int(os.environ.get("LIT_AGENT_CHUNK_MAX_OUTPUT_TOKENS", "0"))))
except (TypeError, ValueError):
    MODEL_CHUNK_MAX_OUTPUT_TOKENS = 0
try:
    MODEL_SYNTHESIS_MAX_OUTPUT_TOKENS = max(0, min(12000, int(os.environ.get("LIT_AGENT_SYNTHESIS_MAX_OUTPUT_TOKENS", "0"))))
except (TypeError, ValueError):
    MODEL_SYNTHESIS_MAX_OUTPUT_TOKENS = 0
try:
    READING_CARD_CHUNK_MAX_OUTPUT_TOKENS = max(0, min(4000, int(os.environ.get("LIT_AGENT_READING_CARD_CHUNK_MAX_OUTPUT_TOKENS", "0"))))
except (TypeError, ValueError):
    READING_CARD_CHUNK_MAX_OUTPUT_TOKENS = 0
try:
    READING_CARD_SYNTHESIS_MAX_OUTPUT_TOKENS = max(0, min(6000, int(os.environ.get("LIT_AGENT_READING_CARD_SYNTHESIS_MAX_OUTPUT_TOKENS", "0"))))
except (TypeError, ValueError):
    READING_CARD_SYNTHESIS_MAX_OUTPUT_TOKENS = 0
MODEL_IMAGE_DETAIL = os.environ.get("LIT_AGENT_IMAGE_DETAIL", "auto").strip().lower()
if MODEL_IMAGE_DETAIL not in {"auto", "low", "high"}:
    MODEL_IMAGE_DETAIL = "auto"
try:
    VISUAL_RENDER_DPI = max(90, min(180, int(os.environ.get("LIT_AGENT_VISUAL_DPI", "120"))))
except (TypeError, ValueError):
    VISUAL_RENDER_DPI = 120
try:
    VISUAL_JPEG_QUALITY = max(55, min(82, int(os.environ.get("LIT_AGENT_VISUAL_JPEG_QUALITY", "68"))))
except (TypeError, ValueError):
    VISUAL_JPEG_QUALITY = 68
RUNTIME_METRICS_LOCK = threading.Lock()
RUNTIME_METRICS = {
    "window_started_at": PROCESS_STARTED_AT,
    "requests_total": 0,
    "errors_4xx": 0,
    "errors_5xx": 0,
    "last_request_at": "",
    "durations_ms": [],
    "routes": {},
}


MODEL_TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504, 524}


# The first relevance layer is deliberately local and explainable. It is a
# triage signal, not a claim that two papers are semantically equivalent.
REL_STOPWORDS = {
    "and", "or", "the", "for", "with", "from", "using", "based", "via", "into", "that",
    "this", "these", "those", "model", "models", "method", "methods", "paper", "study",
    "approach", "towards", "learning", "large", "language", "system", "systems", "data",
    # Chinese function words are especially harmful for long natural-language
    # questions: a provider may return papers because they share “进行” or
    # “机制” while missing the actual object and task.  Domain nouns remain
    # searchable through SEARCH_CONCEPT_ALIASES below.
    "的", "和", "与", "及", "基于", "用于", "研究", "方法", "模型", "学习", "数据",
    "用", "采用", "通过", "进行", "等", "相关", "方面", "实现", "一种", "一种", "如何", "能够",
}

# These are conservative, high-signal research vocabulary bridges.  They are
# used to issue a small number of additional connector queries, never to
# rewrite the user's original query.  Keeping the map local makes the plan
# inspectable and means the feature remains useful without an AI key.
SEARCH_CONCEPT_ALIASES = {
    # Food/health computer-vision vocabulary.  These phrases are common in
    # papers but almost never appear as literal translations in a user's
    # Chinese question, so they must be explicit bridges.
    "食物体积": ("food volume estimation", "food volume measurement", "food portion volume"),
    "食品体积": ("food volume estimation", "food volume measurement", "food portion volume"),
    "体积估计": ("volume estimation", "volumetric estimation", "volume measurement"),
    "体积测量": ("volume measurement", "volumetric measurement", "volume estimation"),
    "食物份量": ("food portion size estimation", "food portion estimation", "portion size measurement"),
    "份量估计": ("portion size estimation", "food portion estimation", "portion size measurement"),
    "食物": ("food", "meal", "dish"),
    "食品": ("food", "meal", "dish"),
    "卷积": ("convolutional neural network", "convolutional", "cnn"),
    "注意力机制": ("attention mechanism", "attention module", "self-attention"),
    "注意力": ("attention mechanism", "attention module", "self-attention"),
    "深度学习": ("deep learning", "deep neural network", "neural network"),
    "图像": ("image-based", "computer vision", "visual"),
    "计算机视觉": ("computer vision", "image-based", "visual recognition"),
    "分割": ("image segmentation", "semantic segmentation", "instance segmentation"),
    "三维重建": ("3d reconstruction", "three-dimensional reconstruction", "3d vision"),
    "深度估计": ("depth estimation", "monocular depth estimation", "depth prediction"),
    "营养评估": ("dietary assessment", "nutrition assessment", "dietary intake estimation"),
    "饮食评估": ("dietary assessment", "food intake estimation", "dietary monitoring"),
    "估计": ("estimation", "measurement", "quantification"),
    "多模态推理": ("multimodal reasoning", "multimodal inference"),
    "多模态": ("multimodal", "multi-modal", "vision-language"),
    "推理": ("reasoning", "inference"),
    "视觉问答": ("visual question answering", "vqa"),
    "视频理解": ("video understanding", "video comprehension"),
    "检索增强生成": ("retrieval augmented generation", "rag"),
    "检索增强": ("retrieval augmented", "retrieval-augmented"),
    "大语言模型": ("large language model", "llm"),
    "视觉语言模型": ("vision-language model", "vlm"),
    "计算机视觉": ("computer vision", "visual recognition"),
    "自然语言处理": ("natural language processing", "nlp"),
    "知识图谱": ("knowledge graph", "knowledge graphs"),
    "图神经网络": ("graph neural network", "gnn"),
    "强化学习": ("reinforcement learning", "rl"),
    "小样本": ("few-shot", "few shot"),
    "零样本": ("zero-shot", "zero shot"),
    "学术检索": ("scholarly search", "academic retrieval"),
    "multimodal": ("multi-modal", "vision-language", "cross-modal"),
    "multi-modal": ("multimodal", "vision-language", "cross-modal"),
    "vision language": ("vision-language", "multimodal"),
    "vision-language": ("vision language", "multimodal"),
    "large language model": ("llm", "foundation model"),
    "large language models": ("llm", "foundation models"),
    "llm": ("large language model", "foundation model"),
    "retrieval augmented generation": ("retrieval-augmented generation", "rag"),
    "retrieval-augmented generation": ("retrieval augmented generation", "rag"),
    "rag": ("retrieval augmented generation", "retrieval-augmented generation"),
    "reinforcement learning": ("rl", "reinforcement-learning"),
    "computer vision": ("visual recognition", "image understanding"),
    "natural language processing": ("nlp", "language understanding"),
    "graph neural network": ("gnn", "graph convolutional network"),
    "generative adversarial network": ("gan", "adversarial generation"),
    "parameter efficient fine tuning": ("parameter-efficient fine-tuning", "peft"),
    "parameter-efficient fine-tuning": ("parameter efficient fine tuning", "peft"),
    "few shot": ("few-shot", "low-resource"),
    "zero shot": ("zero-shot", "open-vocabulary"),
    "智能体": ("agent", "autonomous agent", "ai agent"),
    "人工智能体": ("ai agent", "intelligent agent"),
    "多智能体": ("multi-agent", "multi agent"),
    "长视频": ("long video", "long-form video", "long video understanding"),
    "视频生成": ("video generation", "text-to-video", "generative video"),
    "图像生成": ("image generation", "text-to-image", "generative image"),
    "扩散模型": ("diffusion model", "diffusion models"),
    "提示学习": ("prompt learning", "prompt tuning", "in-context learning"),
    "指令微调": ("instruction tuning", "instruction fine-tuning"),
    "参数高效微调": ("parameter-efficient fine-tuning", "peft"),
    "微调": ("fine-tuning", "fine tuning"),
    "知识增强": ("knowledge augmented", "knowledge-enhanced"),
    "幻觉": ("hallucination", "factuality", "faithfulness"),
    "可解释性": ("explainability", "interpretability"),
    "公平性": ("fairness", "bias evaluation"),
    "鲁棒性": ("robustness", "robustness evaluation"),
    "安全": ("safety", "trustworthy", "red teaming"),
    "基准": ("benchmark", "benchmarking", "evaluation benchmark"),
    "数据集": ("dataset", "datasets", "corpus"),
    "综述": ("survey", "review", "systematic review"),
    "领域自适应": ("domain adaptation", "domain generalization"),
    "跨模态": ("cross-modal", "cross modality"),
    "视觉编码器": ("vision encoder", "visual encoder"),
    "多模态大模型": ("multimodal large language model", "mllm", "multimodal llm"),
    "多模态大语言模型": ("multimodal large language model", "mllm", "multimodal llm"),
}

# Facets let the discovery plan explain *why* a query was expanded and make
# it possible to retrieve adjacent work without replacing the user's wording.
# The map is intentionally small and curated: high precision terms are more
# useful here than a noisy general thesaurus.
SEARCH_FACET_ALIASES = {
    "对象": {
        "食物": ("food", "meal", "dish", "food image"),
        "食品": ("food", "meal", "dish", "food image"),
        "餐食": ("meal", "food", "dish", "food image"),
        "多模态": ("multimodal", "vision-language"),
        "视觉语言模型": ("vision-language model", "vision language model", "vlm"),
        "多模态大模型": ("multimodal large language model", "mllm"),
        "视频": ("video", "video understanding"),
        "图像": ("image", "image understanding"),
        "文本": ("text", "language"),
        "机器人": ("robotics", "robot learning"),
        "医疗": ("medical", "clinical", "healthcare"),
    },
    "方法": {
        "深度学习": ("deep learning", "deep neural network", "neural network"),
        "卷积": ("convolutional neural network", "convolutional", "cnn"),
        "注意力机制": ("attention mechanism", "attention module", "self-attention"),
        "注意力": ("attention mechanism", "attention module", "self-attention"),
        "三维重建": ("3d reconstruction", "3d vision", "three-dimensional reconstruction"),
        "深度估计": ("depth estimation", "monocular depth estimation", "depth prediction"),
        "推理": ("reasoning", "inference"),
        "检索增强": ("retrieval augmented", "retrieval-augmented generation", "rag"),
        "微调": ("fine-tuning", "parameter-efficient fine-tuning"),
        "提示": ("prompting", "prompt tuning", "in-context learning"),
        "扩散": ("diffusion", "diffusion model"),
        "强化学习": ("reinforcement learning", "rl"),
        "知识": ("knowledge", "knowledge graph"),
    },
    "任务": {
        "食物体积": ("food volume estimation", "food volume measurement", "food portion volume"),
        "食品体积": ("food volume estimation", "food volume measurement", "food portion volume"),
        "体积估计": ("volume estimation", "volumetric estimation", "volume measurement"),
        "体积测量": ("volume measurement", "volumetric measurement", "volume estimation"),
        "食物份量": ("food portion size estimation", "food portion estimation", "portion size measurement"),
        "份量估计": ("portion size estimation", "food portion estimation", "portion size measurement"),
        "估计": ("estimation", "measurement", "quantification"),
        "问答": ("question answering", "qa"),
        "生成": ("generation", "generative"),
        "分类": ("classification", "recognition"),
        "检索": ("retrieval", "search"),
        "摘要": ("summarization", "summary"),
        "对齐": ("alignment", "preference optimization"),
        "规划": ("planning", "decision making"),
    },
    "评价": {
        "基准": ("benchmark", "evaluation"),
        "数据集": ("dataset", "benchmark dataset"),
        "鲁棒性": ("robustness", "stress testing"),
        "安全": ("safety", "trustworthiness"),
        "幻觉": ("hallucination", "factuality", "faithfulness"),
    },
}

SEARCH_DEPTHS = {"focused", "balanced", "deep"}
DISCOVERY_MODES = {"balanced", "latest", "foundations", "explore"}


DISCOVERY_FEEDBACK_DECISIONS = {"relevant", "irrelevant", "ignored"}


_SEARCH_RAW_FIELDS = (
    "title", "authors", "year", "abstract", "doi", "arxiv_id", "venue",
    "url", "pdf_url", "external_id", "source", "open_access", "citation_count",
    "type", "verified", "verification_status", "verification_sources",
    "source_citations", "provider_sources", "search_paths", "relevance_label",
    "relevance_reason", "model_provider", "model", "discovery_only", "tavily_score",
)


READING_CARD_FIELDS = (
    "research_question", "contribution", "method", "datasets", "baselines",
    "results", "limitations", "reproducibility", "evidence_notes",
    "relevance_to_me", "next_steps",
)

DEEP_READ_SYNTHESIS_JSON_CONTRACT = {
    "required": ("overview", "research_question", "method", "main_findings"),
    "nonempty": ("overview", "research_question", "main_findings"),
    "any_nonempty": (("method.pipeline", "equation_analysis"),),
}


# ---------------------------------------------------------------------------
# Durable PDF ingestion
# ---------------------------------------------------------------------------


_MODEL_EMPTY_MARKERS = {
    "", "原文未说明", "原文没有说明", "未说明", "未提及", "未报告", "无法确认",
    "待补充", "待填写", "待确认", "请填写", "请补充", "not specified", "not stated",
    "not reported", "not available", "n/a", "na", "tbd", "todo", "研究问题", "方法主线",
    "2-3句论文概述", "overview", "string", "your answer", "your response",
}


MODEL_SEARCH_CONNECTORS = {
    "arxiv": "计算机、数学、物理等领域的预印本和最新工作",
    "openalex": "跨学科论文元数据、摘要、引用和开放获取信息",
    "crossref": "期刊、会议和 DOI 注册元数据",
    "europepmc": "生物医学、生命科学和医学文献",
    "semanticscholar": "跨学科语义检索，公共接口可能限流",
    "dblp": "计算机科学会议和期刊目录",
    "openreview": "机器学习等会议的投稿、评审和录用论文",
    "tavily": "联网发现候选网页；结果必须经过学术身份核验，未核验项只作线索",
}
MODEL_SEARCH_CONNECTOR_LABELS = {
    "arxiv": "arXiv", "openalex": "OpenAlex", "crossref": "Crossref",
    "europepmc": "Europe PMC", "semanticscholar": "Semantic Scholar",
    "dblp": "DBLP", "openreview": "OpenReview", "tavily": "Tavily 联网发现",
}

# These are only probe URLs for the in-memory cooldown circuit.  The actual
# connector functions still build their own request URLs and remain the single
# source of truth for query syntax.  Keeping the mapping here lets an AI-led
# search skip an index that just returned 429 across its subsequent tool turns,
# instead of paying another timeout for a source the circuit already knows is
# unavailable.
MODEL_SEARCH_SOURCE_PROBE_URLS = {
    "arxiv": "https://export.arxiv.org/api/query",
    "openalex": "https://api.openalex.org/works",
    "crossref": "https://api.crossref.org/works",
    "europepmc": "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
    "semanticscholar": "https://api.semanticscholar.org/graph/v1/paper/search",
    "dblp": "https://dblp.org/search/publ/api",
    "openreview": "https://api2.openreview.net/notes",
    "tavily": "https://api.tavily.com/search",
}


MODEL_SEARCH_TOOL_NAME = "search_academic_papers"
try:
    # Model-led search is deliberately bounded: each extra planning turn adds
    # a full provider round trip. The defaults leave room for multiple angles
    # while keeping one-off discovery responsive; power users can raise them
    # through environment variables without changing the application code.
    MODEL_SEARCH_TOOL_MAX_TURNS = max(3, min(7, int(os.environ.get("LIT_MODEL_SEARCH_MAX_TURNS", "6"))))
except (TypeError, ValueError):
    MODEL_SEARCH_TOOL_MAX_TURNS = 6
try:
    MODEL_SEARCH_TOOL_MAX_CALLS = max(4, min(8, int(os.environ.get("LIT_MODEL_SEARCH_MAX_CALLS", "6"))))
except (TypeError, ValueError):
    MODEL_SEARCH_TOOL_MAX_CALLS = 6
MODEL_SEARCH_TOOL_MAX_SOURCES = 3
# A tool call returns a compact page, while the outer search can combine
# several independent calls. Keep enough candidates per call to fill the
# user's requested page after deduplication and relevance review; the hard
# candidate cap still bounds memory and provider work.
MODEL_SEARCH_TOOL_MAX_LIMIT = 12
MODEL_SEARCH_TOOL_MAX_CANDIDATES = 120
# The inventory keeps every fetched record locally, but the model only needs a
# compact shortlist from each tool turn.  Sending all source aliases again in
# the final ranking prompt increases latency and makes structured output more
# likely to be cut off by a relay.  Two requested pages (with a floor of 12)
# preserve ranking context while leaving local backfill as a safe recall path.
MODEL_SEARCH_VISIBLE_CANDIDATE_FLOOR = 12
# Tool responses are evidence for model ranking, not the durable paper record.
# Sending a full 4,000-character abstract for every candidate makes the final
# ranking request needlessly expensive (especially after several parallel
# source calls). Keep a high-signal excerpt in the conversation while the
# complete abstract remains in ``inventory`` and is returned to the UI.
try:
    MODEL_SEARCH_EVIDENCE_ABSTRACT_CHARS = max(
        600, min(4000, int(os.environ.get("LIT_AGENT_SEARCH_EVIDENCE_ABSTRACT_CHARS", "1800")))
    )
except (TypeError, ValueError):
    MODEL_SEARCH_EVIDENCE_ABSTRACT_CHARS = 1800

# The final model-web answer is a small list of candidate IDs, but reasoning
# capable relays may spend the same completion budget on hidden reasoning and
# then truncate the JSON.  Leave the provider's output budget unset by
# default; deployments that need a hard ceiling can opt in explicitly.
try:
    MODEL_SEARCH_MAX_OUTPUT_TOKENS = max(0, min(32000, int(os.environ.get("LIT_AGENT_SEARCH_MAX_OUTPUT_TOKENS", "0"))))
except (TypeError, ValueError):
    MODEL_SEARCH_MAX_OUTPUT_TOKENS = 0

MODEL_WEB_SEARCH_PROMPT = """你是学术文献检索 Agent。你可以通过 search_academic_papers 函数实际检索论文。工具既能查询学术数据库，也能调用 Tavily 做联网发现；Tavily 只提供网页线索，必须优先选择有学术身份核验的结果。

先把自然语言研究问题拆成研究对象、任务、方法/机制、应用场景和边界条件。用领域常用英文术语和同义表达检索；每次工具调用选择 2-3 个来源。Tavily 可用时，对自然语言、跨领域或近期研究问题至少在一轮中同时使用 Tavily 与 1-2 个最匹配的学术来源，利用联网页面发现数据库未召回的候选，再交给 LitLoom 做身份核验。对于宽泛问题可用不同查询角度分轮检索：目标/任务、方法、应用场景、近期工作或基础工作。不要只把整句原样搜索，不要在多轮重复相同查询。若候选重复或无关，换术语、子问题或来源继续搜索；看到足够的相关候选后停止，不要为凑数量无止境调用工具。

同一轮中互相独立的检索角度请尽量一次发起多个 search_academic_papers 调用，LitLoom 会并行查询其中的学术来源；只有需要根据上一轮结果改写查询时才进入下一轮。优先用 2-3 个高信息量角度得到足够候选，不要把同一查询拆成多个串行调用。

avoid_repeats 是“新论文优先”的排序偏好，不是把历史候选全部丢弃，也不是减少结果数量的理由。只要工具返回的候选满足研究对象和任务证据，最终 results 应尽量接近 requested_limit：先放新候选，再选择工具返回中标记 previously_seen=true 的历史候选补足数量，并在 relevance_reason 中说明需要人工复核。不要因为候选以前出现过而主动漏掉它们；只有用户明确要求只看未重复时才保持空缺。若模型主筛选只返回少量，LitLoom 会从本轮真实工具候选中补位，因此请不要把“少选结果”当作去重策略。

用户勾选的来源是优先项，不是硬限制；只有对问题确有帮助时才扩展到其他来源。严格遵守年份、作者、 venue、排除词等约束。判断相关性时关注研究对象与任务是否一致、方法是否真的相关、应用场景是否匹配；排除只共享宽泛关键词的论文。区分 direct、adjacent 和 uncertain，邻近工作要说清迁移关系或差别。Tavily 返回的 verification_status=unverified 或 discovery_only=true 时只能标为 uncertain，不能当作已核验论文；优先保留 arXiv、DOI、Crossref、OpenAlex 等可核验记录。

必须至少调用一次 search_academic_papers 才能给结果。函数返回的 result_id 是唯一可引用候选。只可筛选函数确实返回的 ID，不可编造论文、ID、元数据、链接或搜索记录。最终只输出一个严格 JSON 对象：{"results":[{"result_id":"C1","relevance_label":"direct|adjacent|uncertain|unrelated","relevance_reason":"具体说明关联或差异"}],"gaps":["..."],"next_queries":["..."]}。results 按相关性排序，最多 requested_limit 篇；无关候选不要放入 results。不要在最终 JSON 中重写论文元数据，LitLoom 会从来源记录读取。"""


SOURCE_RANK_WEIGHTS = {
    "openalex": 1.00,
    "semanticscholar": 1.12,
    "arxiv": 0.98,
    "openreview": 1.08,
    "crossref": 0.92,
    "europepmc": 1.00,
    "dblp": 0.90,
    "tavily": 0.72,
}


# ---------------------------------------------------------------------------
# Research-goal Agent runtime
# ---------------------------------------------------------------------------

AGENT_WORKFLOWS = {"research_map", "evidence_review", "follow_up"}
AGENT_RUN_STATUSES = {"queued", "running", "awaiting_confirmation", "success", "partial", "failed", "cancelled", "paused"}
AGENT_STEP_STATUSES = {"queued", "running", "awaiting_confirmation", "success", "failed", "skipped", "cancelled"}

AGENT_TOOL_SPECS = {
    "build_search_plan": {"agent": "PlannerAgent", "class": "Draft", "available": True, "description": "将研究目标拆成检索维度和查询变体"},
    "search_sources": {"agent": "SearchAgent", "class": "Read", "available": True, "description": "调用多源检索并保存候选快照，不自动导入全部论文"},
    "browse_web": {"agent": "BrowserAgent", "class": "Read", "available": True, "description": "通过 Tavily Extract 读取用户指定的公开网页正文；不访问本机地址、不写入文献库"},
    "search_local_library": {"agent": "SearchAgent", "class": "Read", "available": True, "description": "检索本地文献库、阅读卡和页级文本"},
    "resolve_paper_identity": {"agent": "IdentityAgent", "class": "Read", "available": True, "description": "解析 DOI、arXiv 和版本关系"},
    "merge_paper_versions": {"agent": "IdentityAgent", "class": "Read", "available": True, "description": "按 DOI、arXiv、标题、作者和年份识别同一工作的不同版本"},
    "rank_candidates": {"agent": "TriageAgent", "class": "Draft", "available": True, "description": "按相关性、新颖性、来源和重复记忆分层"},
    "evaluate_coverage": {"agent": "CoverageAgent", "class": "Draft", "available": True, "description": "评估研究目标覆盖层、未知项和是否需要补搜"},
    "expand_search": {"agent": "SearchAgent", "class": "Read", "available": True, "description": "仅在覆盖缺口存在时执行一次受限补充检索"},
    "prepare_fulltext": {"agent": "DocumentAgent", "class": "Read", "available": True, "description": "检查本地 PDF、公开下载地址和全文缺口"},
    "download_fulltext": {"agent": "DocumentAgent", "class": "External", "available": True, "requires_confirmation": True, "description": "仅对用户选中的本地论文下载公开 PDF，不自动导入候选"},
    "extract_pdf_pages": {"agent": "DocumentAgent", "class": "Read", "available": True, "description": "提取 PDF 页级文本并记录质量"},
    "ocr_pdf_pages": {"agent": "DocumentAgent", "class": "Read", "available": True, "description": "仅对低质量或空白页执行可选 OCR，并记录降级原因"},
    "render_pdf_page": {"agent": "DocumentAgent", "class": "Read", "available": True, "description": "渲染页面供公式、图表和图片核验"},
    "search_document": {"agent": "DocumentAgent", "class": "Read", "available": True, "description": "在已提取的论文页面中定位术语和证据"},
    # Citation expansion only reads a public index and returns a candidate
    # snapshot.  It does not write the library, so it can run as a bounded
    # Read step inside a plan; the existing paper-details button remains an
    # explicit user action for the interactive UI.
    "expand_citations": {"agent": "SearchAgent", "class": "Read", "available": True, "description": "沿引用、被引和相似关系扩展候选（只读，不自动入库）"},
    "extract_claims": {"agent": "ExtractionAgent", "class": "Draft", "available": True, "description": "从已保存分析和阅读证据中形成 Claim"},
    "verify_evidence": {"agent": "VerifierAgent", "class": "Read", "available": True, "description": "检查 Claim 是否有页码、章节或可回溯证据"},
    "resolve_evidence_gaps": {"agent": "DocumentAgent", "class": "Read", "available": True, "description": "针对未核验 Claim 进行一次受限页面补证据"},
    "critique_claims": {"agent": "CriticAgent", "class": "Draft", "available": True, "description": "寻找反例、冲突、样本偏差、实验缺陷和过度外推"},
    "build_paper_analysis_overview": {"agent": "PaperAnalystAgent", "class": "Read", "available": True, "description": "为单篇论文生成带页码证据的专业分析总览、复现清单和下一步行动"},
    "check_citation_completeness": {"agent": "VerifierAgent", "class": "Read", "available": True, "description": "检查研究产物中的结论是否都有来源 Claim 或证据"},
    "evaluate_stop_conditions": {"agent": "PlannerAgent", "class": "Read", "available": True, "description": "依据覆盖、证据和预算判断是否停止或保留未完成项"},
    "compare_papers": {"agent": "SynthesisAgent", "class": "Draft", "available": True, "description": "按统一维度比较候选论文"},
    "resolve_comparison_gaps": {"agent": "DocumentAgent", "class": "Read", "available": True, "description": "针对比较矩阵缺失字段回到本地页面补证据"},
    "generate_reading_route": {"agent": "SynthesisAgent", "class": "Draft", "available": True, "description": "生成基础、方法、最新和补充阅读顺序"},
    "draft_reading_card": {"agent": "WriterAgent", "class": "Draft", "available": True, "external_data": True, "requires_confirmation": True, "description": "经确认后仅基于全文文字和页码证据生成结构化阅读卡草稿，不发送页面图像，也不覆盖人工内容"},
    "generate_research_brief": {"agent": "WriterAgent", "class": "Draft", "available": True, "description": "生成研究简报或综述草稿"},
    "track_follow_up": {"agent": "FollowUpAgent", "class": "Draft", "available": True, "description": "比较本次发现与研究目标历史，只报告新增、变化和重复论文"},
    "save_reading_card": {"agent": "WriterAgent", "class": "Apply", "available": True, "description": "经确认后写入结构化阅读卡"},
    "export_report": {"agent": "WriterAgent", "class": "External", "available": True, "description": "经确认后导出研究报告"},
}

# Tool contracts are exposed to the UI and API clients so a long-running Agent
# can be inspected before approval. Keep the contracts concrete rather than
# advertising one generic payload for every tool.
AGENT_TOOL_CONTRACTS = {
    "build_search_plan": ({"goal": "object"}, {"query": "string", "query_variants": "array", "sources": "array", "filters": "object"}),
    "search_sources": ({"goal": "object", "query_plan": "object", "limit": "integer"}, {"status": "string", "run_id": "string", "papers": "array", "summary": "object"}),
    "browse_web": ({"urls": "array", "max_chars": "integer"}, {"status": "string", "pages": "array", "failed_urls": "array", "provider": "string"}),
    "search_local_library": ({"query": "string", "limit": "integer"}, {"items": "array", "total": "integer"}),
    "resolve_paper_identity": ({"papers": "array", "limit": "integer"}, {"papers": "array", "resolved_count": "integer", "unresolved_count": "integer"}),
    "merge_paper_versions": ({"goal": "object", "papers": "array", "identity": "object"}, {"groups": "array", "duplicate_count": "integer", "history": "object"}),
    "rank_candidates": ({"papers": "array", "local_context": "object", "limit": "integer"}, {"candidates": "array", "total": "integer", "high_relevance": "integer"}),
    "evaluate_coverage": ({"goal": "object", "triage": "object"}, {"coverage": "object", "gaps": "array", "needs_expansion": "boolean"}),
    "expand_search": ({"goal": "object", "coverage": "object", "limit": "integer"}, {"expanded": "boolean", "papers": "array", "history": "object"}),
    "prepare_fulltext": ({"papers": "array"}, {"papers": "array", "ready_count": "integer", "downloadable_count": "integer", "missing_count": "integer"}),
    "download_fulltext": ({"papers": "array", "paper_ids": "array", "confirm": "boolean"}, {"status": "string", "downloaded": "array", "failed": "array"}),
    "extract_pdf_pages": ({"paper_ids": "array"}, {"papers_processed": "integer", "results": "array", "skipped": "array"}),
    "ocr_pdf_pages": ({"paper_ids": "array", "max_pages": "integer"}, {"status": "string", "attempted": "integer", "updated": "integer"}),
    "render_pdf_page": ({"paper_id": "string", "page": "integer"}, {"paper_id": "string", "page": "integer", "url": "string"}),
    "search_document": ({"paper_id": "string", "query": "string", "limit": "integer"}, {"paper_id": "string", "pages": "array"}),
    "expand_citations": ({"goal": "object", "papers": "array", "limit": "integer"}, {"status": "string", "expanded": "boolean", "papers": "array", "candidate_ids": "array", "history": "object"}),
    "extract_claims": ({"goal": "object", "papers": "array"}, {"claims": "array", "claim_count": "integer"}),
    "verify_evidence": ({"goal": "object", "claims": "array"}, {"results": "array", "claims_checked": "integer", "needs_fulltext": "integer"}),
    "resolve_evidence_gaps": ({"goal": "object", "claims": "array", "verification": "object"}, {"attempted": "integer", "resolved_count": "integer", "remaining_count": "integer"}),
    "critique_claims": ({"claims": "array", "verification": "object"}, {"critiques": "array", "critique_count": "integer", "status": "string"}),
    "build_paper_analysis_overview": ({"papers": "array", "claims": "array", "verification": "object", "critique": "object"}, {"paper_overviews": "array", "overview_count": "integer", "fulltext_count": "integer", "abstract_only_count": "integer", "evidence_count": "integer", "status": "string"}),
    "check_citation_completeness": ({"previous": "object"}, {"claims_total": "integer", "claims_cited": "integer", "complete": "boolean"}),
    "evaluate_stop_conditions": ({"goal": "object", "previous": "object"}, {"stop": "boolean", "reasons": "array", "blocking_reasons": "array"}),
    "compare_papers": ({"papers": "array", "claims": "array"}, {"dimensions": "array", "rows": "array", "gaps": "array"}),
    "resolve_comparison_gaps": ({"papers": "array", "claims": "array", "comparison": "object"}, {"attempted": "integer", "resolved_count": "integer", "remaining_count": "integer"}),
    "generate_reading_route": ({"papers": "array", "claims": "array"}, {"route": "array", "route_count": "integer"}),
    "draft_reading_card": ({"paper": "object", "confirm": "boolean"}, {"status": "string", "reading_card": "object", "prereading_guide": "object", "evidence_scope": "string"}),
    "generate_research_brief": ({"goal": "object", "previous": "object"}, {"artifact_type": "string", "candidates": "array", "claims": "array"}),
    "track_follow_up": ({"previous": "object"}, {"artifact_type": "string", "new_papers": "array", "changed_papers": "array", "repeated_papers": "array"}),
    "save_reading_card": ({"paper_id": "string", "reading_card": "object", "confirm": "boolean"}, {"paper_id": "string", "reading_card": "object", "applied": "boolean"}),
    "export_report": ({"content": "string", "confirm": "boolean"}, {"filename": "string", "content": "string", "content_sha256": "string"}),
}
for _tool_name, _tool_spec in AGENT_TOOL_SPECS.items():
    _tool_input, _tool_output = AGENT_TOOL_CONTRACTS.get(_tool_name, ({"goal": "object", "previous": "object"}, {"status": "string", "data": "object"}))
    _tool_spec.setdefault("input_schema", _tool_input)
    _tool_spec.setdefault("output_schema", _tool_output)
    _tool_spec.setdefault("max_calls", 2 if _tool_name in {"search_sources", "expand_search", "expand_citations", "browse_web"} else 1)
    _tool_spec.setdefault("timeout_seconds", 900 if _tool_name == "draft_reading_card" else (300 if _tool_name in {"search_sources", "expand_search", "extract_pdf_pages"} else 120))
    _tool_spec.setdefault("idempotent", _tool_name not in {"save_reading_card"})
    _tool_spec.setdefault("retryable", _tool_spec["class"] in {"Read", "Draft"})
    _tool_spec.setdefault("external_data", _tool_name in {"search_sources", "expand_search", "expand_citations", "generate_research_brief"} or _tool_spec["class"] in {"External"})
    _tool_spec.setdefault("requires_confirmation", _tool_spec["class"] in {"Apply", "External"} or _tool_name == "generate_research_brief")
    _tool_spec.setdefault("produces", [])


_AGENT_SCHEMA_MISSING = object()


ABSTRACT_LOCAL_FALLBACK_MARKER = "【本地降级：未完成模型推理】"


# Domain modules are composed here to preserve the historic `import app` API.
from literature_desk.model_runtime import *  # noqa: F401,F403
from literature_desk.workspaces import *  # noqa: F401,F403
from literature_desk.database import *  # noqa: F401,F403
from literature_desk.library import *  # noqa: F401,F403
from literature_desk.documents import *  # noqa: F401,F403
from literature_desk.paper_analysis import *  # noqa: F401,F403
from literature_desk.providers import *  # noqa: F401,F403
from literature_desk.discovery import *  # noqa: F401,F403
from literature_desk.jobs import *  # noqa: F401,F403
from literature_desk.research_agents import *  # noqa: F401,F403
from literature_desk.analysis import *  # noqa: F401,F403
from literature_desk.web.handler import *  # noqa: F401,F403
from literature_desk.services import bind_application

bind_application(sys.modules[__name__])
Handler.server_version = f"LiteratureDesk/{APP_VERSION}"

SOURCE_SEARCH = {
    "crossref": search_crossref, "arxiv": search_arxiv, "openalex": search_openalex,
    "europepmc": search_europepmc, "semanticscholar": search_semanticscholar,
    "dblp": search_dblp, "openreview": search_openreview, "tavily": search_tavily,
}

def main():
    global SCHEDULER_STARTED_AT
    startup_findings = RUNTIME_CONFIG.validate(postgres_backend=using_postgres())
    startup_errors = [
        item["message"] for item in startup_findings if item["severity"] == "error"
    ]
    if startup_errors:
        raise RuntimeError("启动配置无效：" + "；".join(startup_errors))
    warnings = [
        item["message"] for item in startup_findings if item["severity"] == "warning"
    ]
    if warnings:
        print(
            json.dumps(
                {"event": "startup.configuration_warning", "warnings": warnings},
                ensure_ascii=False,
            ),
            flush=True,
        )
    # ``main`` is normally called once per process, but clearing this event
    # also makes embedded/restart tests deterministic after a prior graceful
    # shutdown.
    STOP_EVENT.clear()
    if using_postgres():
        postgres_ensure_local_server(postgres_database_url())
    initialize_workspace_databases()
    reconcile_invalid_ai_jobs()
    # Every durable worker is scoped to the active tenant. In single-user mode
    # the helper executes once with the normal global database context.
    resume_workspace_background_jobs()
    SCHEDULER_STARTED_AT = now_iso()
    thread = threading.Thread(target=scheduler_loop, name="paper-query-scheduler", daemon=True)
    thread.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"LitLoom 已启动：http://{HOST}:{PORT}")
    print(f"本地数据目录：{DATA_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP_EVENT.set()
        server.server_close()
        thread.join(timeout=5)
        shutdown_background_workers(wait=True)


if __name__ == "__main__":
    main()
