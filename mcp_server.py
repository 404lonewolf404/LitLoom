"""Dependency-free MCP stdio adapter for a running LitLoom service.

The web application remains the source of truth for data, jobs, retries and
model credentials.  This process only speaks the Model Context Protocol over
stdin/stdout and forwards bounded, auditable calls to LitLoom's local HTTP API.
It deliberately never reads or returns an API key.

Run it from an MCP client with::

    python mcp_server.py

The target can be changed with ``LITLOOM_URL`` (default
``http://127.0.0.1:8772``).  Protocol output is written to stdout; diagnostics
go to stderr so MCP clients never receive log noise as JSON-RPC messages.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from http.client import HTTPResponse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


SERVER_NAME = "litloom"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_BASE_URL = "http://127.0.0.1:8772"
MAX_RESULT_CHARS = 80_000
TERMINAL_SEARCH_STATES = {"success", "partial", "failed", "cancelled", "interrupted"}


def _repair_unicode(value: Any) -> Any:
    """Make values safe for UTF-8 MCP transport on Windows.

    A Windows console using a legacy code page can leave lone UTF-16
    surrogates in strings read from stdio or returned by a relay.  Those
    strings survive ``json.dumps(..., ensure_ascii=False)`` but fail when
    encoded as UTF-8.  Repair valid pairs and replace genuinely unpaired
    units so one malformed field cannot abort a whole tool call.
    """
    if isinstance(value, str):
        try:
            return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        except UnicodeError:
            return value.encode("utf-8", "replace").decode("utf-8", "replace")
    if isinstance(value, list):
        return [_repair_unicode(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_repair_unicode(item) for item in value)
    if isinstance(value, dict):
        return {_repair_unicode(key): _repair_unicode(item) for key, item in value.items()}
    return value


def _base_url() -> str:
    raw = (os.environ.get("LITLOOM_URL") or DEFAULT_BASE_URL).strip()
    if not raw:
        raw = DEFAULT_BASE_URL
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("LITLOOM_URL 必须是 http(s) URL")
    return raw.rstrip("/")


def _timeout(default: float = 30.0) -> float:
    try:
        return max(1.0, min(300.0, float(os.environ.get("LITLOOM_MCP_TIMEOUT_SECONDS", default))))
    except (TypeError, ValueError):
        return default


def _service_token() -> str:
    """Read the optional MCP token from the same local sources as LitLoom."""
    configured_file = os.environ.get("LITLOOM_MCP_TOKEN_FILE", "").strip()
    if configured_file:
        try:
            with open(os.path.expanduser(configured_file), "r", encoding="utf-8") as handle:
                value = handle.read().strip()
        except (OSError, UnicodeError):
            value = ""
        if value and not value.startswith("#"):
            if "=" in value and value.split("=", 1)[0].strip().upper() == "LITLOOM_MCP_TOKEN":
                value = value.split("=", 1)[1].strip().strip("'\"")
            return value
    return os.environ.get("LITLOOM_MCP_TOKEN", "").strip()


def _json_text(value: Any, limit: int = MAX_RESULT_CHARS) -> str:
    """Serialize a tool result without silently returning an invalid payload."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= limit:
        return text
    # Keep the envelope useful when a caller accidentally requests a very
    # large paper list.  The authoritative result remains available through
    # the normal LitLoom API and can be paged with a narrower tool call.
    return json.dumps(
        {
            "truncated": True,
            "message": "结果超过 MCP 单次传输上限，请缩小 limit 或按论文/任务 ID 查询。",
            "preview": text[: max(0, limit - 180)] + "…",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _bounded_result(value: Any, limit: int = MAX_RESULT_CHARS) -> Any:
    """Keep both MCP content and structuredContent within one safe bound."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) <= limit:
        # MCP structuredContent is an object in the current protocol schema;
        # wrap list/scalar REST responses while leaving the human-readable
        # content block unchanged.
        return value if isinstance(value, dict) else {"data": value}
    return {
        "truncated": True,
        "message": "结果超过 MCP 单次传输上限，请缩小 limit 或按论文/任务 ID 查询。",
        "size_chars": len(encoded),
        "max_chars": limit,
    }


def _clean_limit(value: Any, lower: int = 1, upper: int = 50, default: int = 20) -> int:
    try:
        return min(upper, max(lower, int(value)))
    except (TypeError, ValueError):
        return default


def _compact_library_response(value: Any) -> Any:
    """Remove large model answers from library list results.

    The HTTP library endpoint intentionally includes recent artifact metadata
    for the browser, but its ``answer`` fields can contain tens of thousands
    of characters per paper.  MCP callers can fetch a single paper or job
    when they need that payload; list/search results should remain useful and
    below the transport limit instead of being replaced by a generic
    ``truncated`` envelope.
    """
    if isinstance(value, list):
        items = value
        wrapped = False
    elif isinstance(value, dict) and isinstance(value.get("results"), list):
        items = value["results"]
        wrapped = True
    else:
        return value
    compact = []
    for item in items:
        if not isinstance(item, dict):
            compact.append(item)
            continue
        entry = dict(item)
        # The browser's paper list includes the complete structured reading
        # card for convenience.  MCP list/search calls should stay small and
        # predictable; callers can use litloom_get_paper for the full card.
        card = entry.get("reading_card")
        if isinstance(card, dict):
            entry["reading_card"] = {
                "paper_id": card.get("paper_id") or entry.get("id", ""),
                "completed_fields": card.get("completed_fields", 0),
                "field_count": card.get("field_count", 0),
                "completion_pct": card.get("completion_pct", 0),
                "updated_at": card.get("updated_at", ""),
            }
        # Abstracts and free-form notes can also be very large after PDF
        # imports. Preserve a useful preview in list results and keep the
        # authoritative full text behind litloom_get_paper.
        for field, limit in (("abstract", 4000), ("notes", 1200)):
            value = entry.get(field)
            if isinstance(value, str) and len(value) > limit:
                entry[field] = value[:limit] + "…"
                entry[f"{field}_truncated"] = True
        artifacts = entry.get("ai_artifacts")
        if isinstance(artifacts, list):
            compact_artifacts = []
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    compact_artifacts.append(artifact)
                    continue
                metadata = dict(artifact)
                if "answer" in metadata:
                    metadata["answer_available"] = bool(metadata.get("answer"))
                    metadata.pop("answer", None)
                compact_artifacts.append(metadata)
            entry["ai_artifacts"] = compact_artifacts
        compact.append(entry)
    if wrapped:
        result = dict(value)
        result["results"] = compact
        return result
    return compact


def _clip_text(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _compact_string_list(value: Any, item_limit: int, text_limit: int) -> Any:
    if not isinstance(value, list):
        return value
    result = []
    for item in value[:item_limit]:
        if isinstance(item, str):
            result.append(_clip_text(item, text_limit))
        elif isinstance(item, dict):
            compact = {}
            for key in ("source", "title", "name", "url", "doi", "year", "text", "reason", "label", "score"):
                if key in item:
                    compact[key] = _clip_text(item[key], text_limit) if isinstance(item[key], str) else item[key]
            result.append(compact)
        else:
            result.append(item)
    if len(value) > item_limit:
        result.append({"more_items": len(value) - item_limit})
    return result


def _compact_triage(value: Any, reason_limit: int) -> Any:
    if not isinstance(value, dict):
        return value
    compact = {}
    for key in ("id", "status", "label", "score", "confidence", "origin", "reason", "error"):
        if key in value:
            compact[key] = _clip_text(value[key], reason_limit) if isinstance(value[key], str) else value[key]
    return compact


def _compact_search_candidate(value: Any, *, abstract_limit: int, reason_limit: int) -> Any:
    if not isinstance(value, dict):
        return value
    fields = (
        "id", "title", "authors", "year", "venue", "doi", "arxiv_id", "url", "pdf_url",
        "source", "academic_source", "status", "topic", "abstract", "discovery_only",
        "source_attested", "verified", "verification_status", "relevance_reason", "model_relevance_label",
        "relevance_score", "relevance_label", "match_reason", "retrieval_relevance_score",
        "retrieval_relevance_label", "seen_before", "seen_status", "review_decision", "imported_paper_id",
        "discovery_class", "discovery_score", "novelty_score", "search_rank", "source_count",
        "citation_count", "open_access", "intent_key",
    )
    compact = {key: value[key] for key in fields if key in value}
    for key, limit in (
        ("title", 300), ("authors", 300), ("venue", 180), ("doi", 180), ("arxiv_id", 100),
        ("url", 300), ("pdf_url", 300), ("topic", 200), ("relevance_reason", reason_limit),
        ("match_reason", reason_limit),
    ):
        if key in compact:
            compact[key] = _clip_text(compact[key], limit)
    if "abstract" in compact:
        original = compact["abstract"]
        compact["abstract"] = _clip_text(original, abstract_limit)
        if isinstance(original, str) and len(original) > abstract_limit:
            compact["abstract_truncated"] = True
    for key in ("relevance_reasons", "retrieval_relevance_reasons", "discovery_reasons"):
        if key in value:
            compact[key] = _compact_string_list(value[key], 3, reason_limit)
    for key in ("discovery_lanes", "tags", "sources", "verification_sources"):
        if key in value:
            compact[key] = _compact_string_list(value[key], 5, 160)
    for key in ("ai_triage", "ai_screening"):
        if key in value:
            compact[key] = _compact_triage(value[key], reason_limit)
    if "source_citations" in value:
        compact["source_citations"] = _compact_string_list(value["source_citations"], 3, 240)
    return compact


def _compact_source_status(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compact = {key: value[key] for key in ("source", "status", "fetched", "accepted", "filtered", "variant_duplicates", "new", "duplicates", "error") if key in value}
    if "error" in compact:
        compact["error"] = _clip_text(compact["error"], 1200)
    variants = value.get("variants")
    if isinstance(variants, list):
        compact["variants"] = [
            {key: _clip_text(item[key], 240) if isinstance(item.get(key), str) else item[key] for key in ("id", "query", "kind", "fetched", "accepted", "filtered") if key in item}
            for item in variants[:20]
            if isinstance(item, dict)
        ]
        if len(variants) > 20:
            compact["variant_count_omitted"] = len(variants) - 20
    return compact


def _compact_search_snapshot(snapshot: Any, *, offset: int = 0, limit: int = 20) -> Any:
    """Keep every search candidate retrievable without exceeding MCP's payload cap.

    Search snapshots repeat candidate metadata in several ranking fields. The
    full HTTP response is useful to the browser, but sending it verbatim over
    MCP can exceed MAX_RESULT_CHARS and replace all results with a truncation
    notice. Project each candidate to review-relevant fields and page only the
    candidate list; search totals and screening metadata always describe the
    full run.
    """
    if not isinstance(snapshot, dict):
        return snapshot
    papers = snapshot.get("papers")
    if not isinstance(papers, list):
        return snapshot
    total = len(papers)
    offset = max(0, min(total, int(offset or 0)))
    limit = _clean_limit(limit, 1, 50, 20)
    selected = papers[offset: offset + limit]
    profiles = ((1800, 1200), (900, 600), (360, 240), (160, 140))
    for abstract_limit, reason_limit in profiles:
        result = {key: value for key, value in snapshot.items() if key not in {"papers", "run"}}
        result["papers"] = [
            _compact_search_candidate(item, abstract_limit=abstract_limit, reason_limit=reason_limit)
            for item in selected
        ]
        result["candidate_total"] = total
        result["papers_offset"] = offset
        result["papers_returned"] = len(selected)
        result["more_papers_available"] = offset + len(selected) < total
        result["next_offset"] = offset + len(selected) if offset + len(selected) < total else None
        run = snapshot.get("run")
        if isinstance(run, dict):
            run_fields = (
                "id", "status", "started_at", "completed_at", "new_count", "duplicate_count", "stage",
                "attempts", "next_retry_at", "last_error", "current_source", "source_index", "source_total",
                "progress_current", "progress_total", "candidate_mode", "candidate_count", "imported_count",
                "awaiting_review_count", "triage", "ai_screening", "screening_status", "screening_progress",
                "workflow_status",
            )
            result["run"] = {key: run[key] for key in run_fields if key in run}
        source_rows = snapshot.get("sources")
        if isinstance(source_rows, list):
            result["sources"] = [_compact_source_status(item) for item in source_rows]
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded) + 180 <= MAX_RESULT_CHARS:
            result["mcp_compacted"] = True
            result["mcp_compaction_note"] = "候选按审阅字段精简；abstract_truncated 标记表示摘要被截短，理由文本也可能缩短；候选列表可用 offset/limit 分页读取。"
            return result
    # Preserve the whole candidate count and a usable first candidate page
    # rather than silently returning no results when an unusually large run
    # still exceeds the transport limit.
    result["papers"] = result["papers"][: max(1, min(len(result["papers"]), 5))]
    result["papers_returned"] = len(result["papers"])
    result["more_papers_available"] = offset + len(result["papers"]) < total
    result["next_offset"] = offset + len(result["papers"]) if result["more_papers_available"] else None
    result["mcp_compacted"] = True
    result["mcp_compaction_note"] = "候选较多，使用 offset/limit 分页读取完整列表。"
    return result


def _compact_search_tool_result(name: str, value: Any, args: dict | None = None) -> Any:
    args = args if isinstance(args, dict) else {}
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    limit = _clean_limit(args.get("limit"), 1, 50, 20)
    if name == "litloom_search_papers" and isinstance(value, dict) and isinstance(value.get("search"), dict):
        result = dict(value)
        result["search"] = _compact_search_snapshot(value["search"], offset=offset, limit=limit)
        return result
    return _compact_search_snapshot(value, offset=offset, limit=limit)


@dataclass
class LitLoomHTTPError(Exception):
    status: int
    message: str
    payload: Any = None

    def __str__(self) -> str:
        return f"LitLoom HTTP {self.status}: {self.message}"


class LitLoomClient:
    """Small JSON client with no dependency on LitLoom internals or secrets."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or _base_url()).rstrip("/")

    def _request(self, method: str, path: str, payload: dict | None = None, timeout: float | None = None) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        body = None
        headers = {"Accept": "application/json", "User-Agent": "LitLoom-MCP/" + SERVER_VERSION}
        service_token = _service_token()
        if service_token:
            headers["Authorization"] = "Bearer " + service_token
        if payload is not None:
            body = json.dumps(_repair_unicode(payload), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(self.base_url + path, data=body, method=method.upper(), headers=headers)
        try:
            with urlopen(request, timeout=timeout or _timeout()) as response:
                raw = response.read().decode("utf-8", "replace")
                return _repair_unicode(json.loads(raw)) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            try:
                payload_value = _repair_unicode(json.loads(raw)) if raw else None
            except (TypeError, ValueError):
                payload_value = None
            message = payload_value.get("error") if isinstance(payload_value, dict) else raw
            raise LitLoomHTTPError(exc.code, str(message or exc.reason or "HTTP 请求失败"), payload_value) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise LitLoomHTTPError(0, f"无法连接 LitLoom：{exc}") from exc

    def get(self, path: str, timeout: float | None = None) -> Any:
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, payload: dict | None = None, timeout: float | None = None) -> Any:
        return self._request("POST", path, payload=payload or {}, timeout=timeout)


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None, *, read_only: bool = True, open_world: bool = False) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": False,
            "idempotentHint": read_only,
            "openWorldHint": open_world,
        },
    }


TOOLS = [
    _tool(
        "litloom_health",
        "检查 LitLoom 本地服务、数据库和调度器状态；不会访问模型或返回密钥。",
        {},
    ),
    _tool(
        "litloom_search_papers",
        "在 LitLoom 中启动一次可恢复的论文检索。结果先进入候选审阅区，不会自动加入文献库。可选学术来源，也可显式选择 Tavily 或已配置的 DeepSeek Search。调用后用 litloom_search_status 查询结果。",
        {
            "query": {"type": "string", "description": "自然语言研究问题或检索目标"},
            "mode": {"type": "string", "enum": ["model_web", "connectors"], "description": "model_web 让模型选择检索角度和工具；connectors 使用指定学术来源"},
            "sources": {"type": "array", "items": {"type": "string", "enum": ["arxiv", "openalex", "crossref", "europepmc", "semanticscholar", "dblp", "openreview", "tavily", "deepseek_search"]}, "description": "connectors 模式可选择学术来源或已配置的联网搜索；model_web 可留空让模型自主选择"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 30, "default": 0, "description": "启动后最多等待几秒并返回当前状态；长任务建议用状态工具轮询"},
        },
        ["query"],
        read_only=False,
        open_world=True,
    ),
    _tool(
        "litloom_search_status",
        "读取持久化论文检索任务、AI 初筛状态和候选结果。候选较多时可用 offset/limit 分页；页面切换或服务重启后仍可查询。",
        {
            "run_id": {"type": "string", "description": "litloom_search_papers 返回的运行 ID"},
            "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "候选分页起始位置"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20, "description": "本次返回的候选数；完整候选总数会单独返回"},
        },
        ["run_id"],
    ),
    _tool(
        "litloom_browse_web",
        "通过已配置的 Tavily Extract 读取多个指定公开网页正文；服务端会拒绝本机/私网地址，结果仅作为外部网页证据，不会自动写入文献库。",
        {
            "urls": {"type": "array", "items": {"type": "string", "format": "uri"}, "minItems": 1, "maxItems": 3},
            "max_chars": {"type": "integer", "minimum": 500, "maximum": 20000, "default": 12000},
        },
        ["urls"],
        open_world=True,
    ),
    _tool(
        "litloom_fetch_web_page",
        "通过已配置的 DeepSeek Fetch 将单个公开网页抓取为 Markdown；服务端会拒绝本机/私网地址，结果仅作为外部网页证据，不会自动写入文献库。",
        {
            "url": {"type": "string", "format": "uri", "description": "公开网页 HTTP(S) 地址"},
            "max_chars": {"type": "integer", "minimum": 500, "maximum": 20000, "default": 12000},
        },
        ["url"],
        open_world=True,
    ),
    _tool(
        "litloom_search_history",
        "读取历史检索记录，用于避免重复搜索并恢复过去的候选结果。",
        {"query": {"type": "string", "description": "可选的历史问题过滤词"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
    ),
    _tool(
        "litloom_get_recommendations",
        "读取基于本地研究画像以及 OpenAlex、Semantic Scholar 官方推荐服务的下一步论文建议；外部候选只展示，不会自动加入文献库。",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}},
    ),
    _tool(
        "litloom_search_library",
        "检索已经明确加入 LitLoom 文献库的论文。",
        {"query": {"type": "string", "description": "标题、作者、摘要、笔记或全文检索词"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}, "offset": {"type": "integer", "minimum": 0, "default": 0}},
    ),
    _tool(
        "litloom_get_paper",
        "读取一篇论文的元数据、阅读卡和已保存分析摘要（不返回 API 密钥）。",
        {"paper_id": {"type": "string"}},
        ["paper_id"],
    ),
    _tool(
        "litloom_build_paper_overview",
        "读取一篇已入库论文的专业分析总览：研究问题、方法、实验、证据范围、批判风险、复现缺口和下一步。只使用本地已保存的 PDF 页面文本、摘要和 Claim，不自动下载论文、不调用模型。",
        {"paper_id": {"type": "string"}},
        ["paper_id"],
    ),
    _tool(
        "litloom_search_document",
        "在论文已提取的逐页 PDF 文本中查找关键词或语义概念并返回页码证据。semantic=true 时使用已建立的 Embedding + BM25 混合索引；未配置或不可用时由服务明确回退 BM25。",
        {"paper_id": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}, "semantic": {"type": "boolean", "default": False, "description": "Use semantic + BM25 retrieval when the local paper index is ready"}},
        ["paper_id", "query"],
    ),
    _tool(
        "litloom_semantic_index_status",
        "读取本地 PDF 语义索引覆盖、过期页面和后台任务进度；不调用模型。",
        {"paper_id": {"type": "string"}},
        ["paper_id"],
    ),
    _tool(
        "litloom_build_semantic_index",
        "为一篇论文异步建立或更新 Embedding 语义索引。会把已提取的 PDF 文字发送到配置的 Embedding 服务；必须先取得用户对本次外发的明确同意。",
        {"paper_id": {"type": "string"}, "consent": {"type": "boolean", "description": "必须为 true，表示已获准发送该论文提取文字到 Embedding 服务"}},
        ["paper_id", "consent"],
        read_only=False,
        open_world=True,
    ),
    _tool(
        "litloom_start_analysis",
        "为文献启动持久化 AI 分析任务。必须显式确认允许将该论文内容发送到已配置模型；任务可在刷新、切页或重启后继续。",
        {
            "paper_id": {"type": "string"},
            "kind": {"type": "string", "enum": ["abstract_brief", "reading_card", "deep_read", "prereading_guide"], "default": "reading_card"},
            "consent": {"type": "boolean", "description": "必须为 true，表示已允许发送论文内容"},
            "language": {"type": "string", "default": "zh-CN"},
            "apply": {"type": "boolean", "default": True, "description": "阅读卡是否自动填入当前为空的结构化字段"},
        },
        ["paper_id", "consent"],
        read_only=False,
        open_world=True,
    ),
    _tool(
        "litloom_get_ai_job",
        "读取 AI 阅读卡、读前准备或精读任务的持久化进度、错误和结果。",
        {"job_id": {"type": "string"}},
        ["job_id"],
    ),
    _tool(
        "litloom_retry_ai_job",
        "重试一个失败或部分完成的 AI 任务；已有部分结果会被保留。",
        {"job_id": {"type": "string"}},
        ["job_id"],
        read_only=False,
    ),
]


def _query_path(value: Any) -> str:
    return quote(str(value or ""), safe="")


def _poll_search(client: LitLoomClient, run_id: str, wait_seconds: float) -> Any:
    wait_seconds = max(0.0, min(30.0, float(wait_seconds or 0)))
    deadline = time.monotonic() + wait_seconds
    latest = client.get(f"/api/runs/{_query_path(run_id)}/results")
    while wait_seconds and time.monotonic() < deadline and isinstance(latest, dict) and latest.get("status") not in TERMINAL_SEARCH_STATES:
        time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
        latest = client.get(f"/api/runs/{_query_path(run_id)}/results")
    return latest


def _enrich_search_status(snapshot: Any) -> Any:
    """Expose the second (AI triage) phase beside the connector run state."""
    if not isinstance(snapshot, dict):
        return snapshot
    result = dict(snapshot)
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    triage = summary.get("ai_screening") or summary.get("triage")
    if isinstance(triage, dict):
        # Keep the original HTTP-compatible status for callers that already
        # understand it, while making it impossible to mistake a completed
        # connector run for a completed AI screening pass.
        result["search_status"] = result.get("status", "")
        result["screening_status"] = triage.get("status", "")
        result["screening_progress"] = {
            "processed": triage.get("processed_count", triage.get("screened", 0)),
            "total": triage.get("candidate_count", triage.get("total", 0)),
            "error": triage.get("error", ""),
        }
        if result.get("status") in TERMINAL_SEARCH_STATES and triage.get("status") in {"queued", "running"}:
            result["workflow_status"] = "screening_running"
        elif triage.get("status") in TERMINAL_SEARCH_STATES:
            result["workflow_status"] = triage.get("status")
        else:
            result["workflow_status"] = result.get("status", "")
    else:
        result.setdefault("workflow_status", result.get("status", ""))
    return result


def call_tool(name: str, args: dict | None = None, client: LitLoomClient | None = None) -> Any:
    """Execute one allow-listed tool and return a JSON-serializable value."""
    args = args if isinstance(args, dict) else {}
    client = client or LitLoomClient()
    if name == "litloom_health":
        return client.get("/api/health")
    if name == "litloom_search_papers":
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query 不能为空")
        mode = str(args.get("mode") or "model_web").strip().lower()
        if mode not in {"model_web", "connectors"}:
            raise ValueError("mode 必须是 model_web 或 connectors")
        limit = _clean_limit(args.get("limit"), 1, 50, 20)
        raw_sources = args.get("sources") if isinstance(args.get("sources"), list) else []
        allowed = {"arxiv", "openalex", "crossref", "europepmc", "semanticscholar", "dblp", "openreview", "tavily", "deepseek_search"}
        sources = list(dict.fromkeys(str(item) for item in raw_sources if str(item) in allowed))
        if mode == "connectors" and not sources:
            sources = ["arxiv", "openalex", "crossref"]
        options = {
            "search_backend": mode,
            "parallel_sources": True,
            "avoid_repeats": True,
            "fill_repeats": True,
            "suppress_seen": False,
            "search_depth": "balanced",
            "model_web_fallback_to_connectors": True,
        }
        started = client.post("/api/discover", {"query": query, "sources": sources, "limit": limit, "options": options})
        run_id = started.get("id") or started.get("run_id") if isinstance(started, dict) else ""
        if not run_id:
            return {"status": "failed", "error": "LitLoom 未返回检索运行 ID", "response": started}
        status = _enrich_search_status(_poll_search(client, run_id, float(args.get("wait_seconds") or 0)))
        return {"run_id": run_id, "status": status.get("status", "queued") if isinstance(status, dict) else "queued", "search": status}
    if name == "litloom_search_status":
        run_id = str(args.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("run_id 不能为空")
        return _enrich_search_status(client.get(f"/api/runs/{_query_path(run_id)}/results"))
    if name == "litloom_browse_web":
        urls = args.get("urls") if isinstance(args.get("urls"), list) else []
        urls = [str(value).strip() for value in urls if str(value).strip()][:3]
        if not urls:
            raise ValueError("urls 不能为空")
        try:
            max_chars = max(500, min(20000, int(args.get("max_chars") or 12000)))
        except (TypeError, ValueError):
            max_chars = 12000
        return client.post("/api/agent-tools/browse-web", {"urls": urls, "max_chars": max_chars})
    if name == "litloom_fetch_web_page":
        url = str(args.get("url") or "").strip()
        if not url:
            raise ValueError("url 不能为空")
        try:
            max_chars = max(500, min(20000, int(args.get("max_chars") or 12000)))
        except (TypeError, ValueError):
            max_chars = 12000
        return client.post(
            "/api/agent-tools/fetch-web-page",
            {"url": url, "max_chars": max_chars},
        )
    if name == "litloom_search_history":
        limit = _clean_limit(args.get("limit"), 1, 50, 10)
        query = str(args.get("query") or "").strip()
        suffix = "?" + urlencode({"limit": limit, "q": query})
        return client.get("/api/discovery/history" + suffix)
    if name == "litloom_get_recommendations":
        limit = _clean_limit(args.get("limit"), 1, 20, 8)
        return client.get("/api/recommendations?" + urlencode({"limit": limit}))
    if name == "litloom_search_library":
        query = str(args.get("query") or "").strip()
        limit = _clean_limit(args.get("limit"), 1, 100, 20)
        try:
            offset = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        suffix = "?" + urlencode({"q": query, "limit": limit, "offset": offset})
        return _compact_library_response(client.get("/api/papers" + suffix))
    if name == "litloom_get_paper":
        paper_id = str(args.get("paper_id") or "").strip()
        if not paper_id:
            raise ValueError("paper_id 不能为空")
        return client.get(f"/api/papers/{_query_path(paper_id)}?view=summary")
    if name == "litloom_build_paper_overview":
        paper_id = str(args.get("paper_id") or "").strip()
        if not paper_id:
            raise ValueError("paper_id 不能为空")
        return client.get(f"/api/papers/{_query_path(paper_id)}/agent-overview")
    if name == "litloom_search_document":
        paper_id = str(args.get("paper_id") or "").strip()
        query = str(args.get("query") or "").strip()
        if not paper_id or not query:
            raise ValueError("paper_id 和 query 不能为空")
        limit = _clean_limit(args.get("limit"), 1, 50, 20)
        return client.get(f"/api/papers/{_query_path(paper_id)}/pages?" + urlencode({"q": query, "limit": limit, "semantic": "1" if args.get("semantic") is True else "0"}))
    if name == "litloom_semantic_index_status":
        paper_id = str(args.get("paper_id") or "").strip()
        if not paper_id:
            raise ValueError("paper_id 不能为空")
        return client.get(f"/api/papers/{_query_path(paper_id)}/semantic-index")
    if name == "litloom_build_semantic_index":
        paper_id = str(args.get("paper_id") or "").strip()
        if not paper_id:
            raise ValueError("paper_id 不能为空")
        if args.get("consent") is not True:
            raise PermissionError("必须显式设置 consent=true，才能将论文文字发送到 Embedding 服务")
        return client.post(f"/api/papers/{_query_path(paper_id)}/semantic-index", {"consent": True})
    if name == "litloom_start_analysis":
        paper_id = str(args.get("paper_id") or "").strip()
        if not paper_id:
            raise ValueError("paper_id 不能为空")
        if args.get("consent") is not True:
            raise PermissionError("必须显式设置 consent=true 才能向模型发送论文内容")
        kind = str(args.get("kind") or "reading_card").strip().lower()
        endpoint = {
            "abstract_brief": "agent-brief",
            "reading_card": "ai-reading-card",
            "deep_read": "ai-deep-read",
            "prereading_guide": "ai-prereading-guide",
        }.get(kind)
        if not endpoint:
            raise ValueError("未知分析类型")
        payload = {
            "background": True,
            "consent": True,
            "language": str(args.get("language") or "zh-CN"),
            "apply": bool(args.get("apply", True)),
            "merge_empty": True,
        }
        return client.post(f"/api/papers/{_query_path(paper_id)}/{endpoint}", payload)
    if name == "litloom_get_ai_job":
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("job_id 不能为空")
        return client.get(f"/api/ai-jobs/{_query_path(job_id)}")
    if name == "litloom_retry_ai_job":
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("job_id 不能为空")
        return client.post(f"/api/ai-jobs/{_query_path(job_id)}/retry", {})
    raise ValueError(f"未知 MCP 工具：{name}")


def _protocol_error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def dispatch(message: dict, client: LitLoomClient | None = None) -> dict | None:
    """Dispatch one MCP JSON-RPC message. Notifications intentionally return None."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _protocol_error(message.get("id") if isinstance(message, dict) else None, -32600, "无效 JSON-RPC 请求")
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if request_id is None:
        # Notifications (including initialized/cancelled) require no response.
        return None
    if method == "initialize":
        requested = params.get("protocolVersion") or PROTOCOL_VERSION
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": requested, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}, "instructions": "LitLoom 的搜索结果先进入候选审阅区；使用状态工具读取异步任务。"}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if not isinstance(name, str) or name not in {tool["name"] for tool in TOOLS}:
            return _protocol_error(request_id, -32602, "未知工具")
        try:
            result = call_tool(name, args, client=client)
            if name in {"litloom_search_papers", "litloom_search_status"}:
                result = _compact_search_tool_result(name, result, args)
            bounded = _bounded_result(result)
            text = _json_text(bounded)
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}], "structuredContent": bounded, "isError": False}}
        except PermissionError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
        except (ValueError, LitLoomHTTPError) as exc:
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
        except Exception as exc:  # keep the MCP process alive for later calls
            _log(f"tool {name} failed: {exc}")
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": f"工具执行失败：{exc}"}], "isError": True}}
    return _protocol_error(request_id, -32601, f"不支持的 MCP 方法：{method}")


def _log(message: str) -> None:
    print(f"[litloom-mcp] {message}", file=sys.stderr, flush=True)


def _read_message(stream) -> dict | None:
    """Read newline JSON and the older Content-Length framing.

    The official Python MCP transport currently uses newline-delimited JSON;
    accepting Content-Length as well makes this adapter usable with older
    clients without changing the normal path.
    """
    first = stream.readline()
    if not first:
        return None
    if isinstance(first, bytes):
        first = first.decode("utf-8", "replace")
    first = first.strip()
    if not first:
        return _read_message(stream)
    if first.lower().startswith("content-length:"):
        try:
            length = int(first.split(":", 1)[1].strip())
        except (TypeError, ValueError):
            raise ValueError("无效 Content-Length")
        while True:
            header = stream.readline()
            if not header or (header in {b"\r\n", b"\n", "\r\n", "\n"}):
                break
        payload = stream.read(length)
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        return _repair_unicode(json.loads(payload))
    return _repair_unicode(json.loads(first))


def _write_message(stream, value: dict) -> None:
    payload = (json.dumps(_repair_unicode(value), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    # MCP stdio is UTF-8 regardless of the host console code page.  Windows
    # often exposes ``sys.stdout`` as cp936; writing the Unicode string
    # directly then fails on symbols such as minus signs or non-GBK terms.
    # Use the underlying binary stream for the real process and retain a text
    # fallback for unit tests/StringIO callers.
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        binary.write(payload)
        binary.flush()
    else:
        stream.write(payload.decode("utf-8"))
        stream.flush()


def main() -> None:
    # MCP stdio is defined as UTF-8.  On Windows ``sys.stdin`` may inherit
    # the active console code page (often cp936), while PowerShell pipes UTF-8
    # bytes.  Reconfigure the text wrapper before reading so direct Chinese
    # JSON requests are decoded correctly instead of becoming mojibake.  The
    # binary stdout path in ``_write_message`` remains the authoritative UTF-8
    # transport for MCP clients.
    for stream in (sys.stdin, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (LookupError, ValueError):
                pass
    # Fail before accepting protocol messages when the target is malformed;
    # this produces a clear stderr diagnostic instead of a confusing loop.
    try:
        client = LitLoomClient()
    except ValueError as exc:
        _log(str(exc))
        raise SystemExit(2)
    _log(f"connected adapter target {client.base_url}")
    while True:
        try:
            message = _read_message(sys.stdin)
        except (ValueError, json.JSONDecodeError) as exc:
            _log(f"invalid message: {exc}")
            _write_message(sys.stdout, _protocol_error(None, -32700, "无效 JSON"))
            continue
        if message is None:
            return
        try:
            response = dispatch(message, client=client)
            if response is not None:
                _write_message(sys.stdout, response)
        except Exception as exc:
            _log(f"protocol dispatch failed: {exc}")
            if isinstance(message, dict) and message.get("id") is not None:
                _write_message(sys.stdout, _protocol_error(message.get("id"), -32603, "内部错误"))


if __name__ == "__main__":
    main()
