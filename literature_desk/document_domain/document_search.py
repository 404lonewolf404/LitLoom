"""Document page search and semantic indexing."""

from __future__ import annotations

from pathlib import Path

from ..services import app_services as _services

def _document_search_terms(query: str) -> tuple[list[str], list[str]]:
    primary = list(
        dict.fromkeys(
            _services().re.findall(
                r"[A-Za-z0-9][A-Za-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", query.casefold()
            )
        )
    )
    bridges = []
    lowered = query.casefold()
    for concept, aliases in _services().SEARCH_CONCEPT_ALIASES.items():
        if concept.casefold() in lowered:
            bridges.extend(aliases[:3])
    bridge_terms = list(
        dict.fromkeys(
            _services().clean_text(value, 80).casefold()
            for value in bridges
            if _services().clean_text(value, 80)
        )
    )
    return primary[:12], bridge_terms[:12]


def _document_fts_expression(terms: list[str], operator: str) -> str:
    quoted = []
    for term in terms:
        value = term.replace('"', '""')
        if _services().re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,}", value):
            quoted.append(f'"{value}"*')
        else:
            quoted.append(f'"{value}"')
    return f" {operator} ".join(quoted)


def _document_casefold_span(full_text: str, term: str) -> tuple[int, int]:
    folded_term = term.casefold()
    if not folded_term:
        return -1, -1
    position = full_text.casefold().find(folded_term)
    if position < 0:
        return -1, -1

    def source_offset(folded_offset: int) -> int:
        folded_cursor = 0
        for index, char in enumerate(full_text):
            next_cursor = folded_cursor + len(char.casefold())
            if folded_offset < next_cursor:
                return index
            if folded_offset == next_cursor:
                return index + 1
            folded_cursor = next_cursor
        return len(full_text)

    start = source_offset(position)
    end = source_offset(position + len(folded_term))
    return start, max(start, end)


def _document_search_snippet(
    full_text: str, query_terms: list[str], query: str
) -> tuple[str, int, int, list[str]]:
    lowered = full_text.casefold()
    matched = [term for term in query_terms if term and term in lowered]
    spans = [
        _services()._document_casefold_span(full_text, term)
        for term in ([query] if query else []) + matched
    ]
    positions = [(start, end - start) for start, end in spans if start >= 0]
    if not positions:
        start, end = 0, min(len(full_text), 240)
        return full_text[start:end], start, end, matched
    position, match_length = min(positions, key=lambda item: item[0])
    start = max(0, position - 90)
    end = min(len(full_text), position + max(match_length, 1) + 150)
    snippet = (
        ("…" if start else "")
        + full_text[start:end]
        + ("…" if end < len(full_text) else "")
    )
    return (
        snippet[:262],
        position,
        min(len(full_text), position + max(match_length, 1)),
        matched,
    )


def search_document_pages(paper_id: str, query: str, limit: int = 20) -> list[dict]:
    """Rank page evidence with FTS5 BM25 and return stable PDF text anchors."""
    query = _services().clean_text(query, 300)
    if not query:
        raise ValueError("请输入页码检索词")
    limit = min(max(int(limit), 1), 100)
    primary_terms, bridge_terms = _services()._document_search_terms(query)
    lanes = []
    if primary_terms:
        lanes.append(
            (
                "bm25_all_terms",
                _services()._document_fts_expression(primary_terms, "AND"),
                primary_terms,
            )
        )
        if len(primary_terms) > 1:
            lanes.append(
                (
                    "bm25_any_term",
                    _services()._document_fts_expression(primary_terms, "OR"),
                    primary_terms,
                )
            )
    if bridge_terms:
        lanes.append(
            (
                "concept_bridge",
                _services()._document_fts_expression(bridge_terms, "OR"),
                bridge_terms,
            )
        )

    found = {}
    with _services().DB_LOCK, _services().db_session() as db:
        has_fts = _services().database_table_exists(db, "paper_page_search")
        if has_fts:
            for lane, expression, terms in lanes:
                try:
                    matches = db.execute(
                        """SELECT dp.id AS page_id,dp.attachment_id,dp.page_number,dp.text,dp.char_count,
                                  dp.quality,dp.extractor,dp.text_sha256,dp.extracted_at,
                                  bm25(paper_page_search) AS bm25_score
                           FROM paper_page_search
                           JOIN paper_page_search_content ppc ON ppc.rowid=paper_page_search.rowid
                           JOIN document_pages dp ON dp.id=ppc.page_id
                           WHERE paper_page_search MATCH ? AND ppc.paper_id=?
                           ORDER BY bm25_score ASC,dp.page_number ASC,dp.attachment_id ASC
                           LIMIT ?""",
                        (expression, paper_id, min(limit * 4, 400)),
                    ).fetchall()
                except _services().sqlite3.OperationalError:
                    continue
                for row in matches:
                    item = dict(row)
                    page_id = item.pop("page_id")
                    score = float(item.pop("bm25_score") or 0)
                    previous = found.get(page_id)
                    if previous is None or score < previous["_score"]:
                        item["_score"] = score
                        item["retrieval_method"] = lane
                        item["_matched_terms"] = terms
                        found[page_id] = item
                if len(found) >= limit:
                    break

        if len(found) < limit:
            escaped = (
                query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            fallback = db.execute(
                """SELECT id AS page_id,attachment_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at
                   FROM document_pages WHERE paper_id=? AND text LIKE ? ESCAPE '\\'
                   ORDER BY page_number,attachment_id LIMIT ?""",
                (paper_id, f"%{escaped}%", limit),
            ).fetchall()
            for row in fallback:
                item = dict(row)
                page_id = item.pop("page_id")
                found.setdefault(
                    page_id,
                    {
                        **item,
                        "retrieval_method": "exact_phrase",
                        "_matched_terms": [query],
                        "_score": 0.0,
                    },
                )

    ordered = sorted(
        found.values(),
        key=lambda item: (
            (
                0
                if item["retrieval_method"] == "bm25_all_terms"
                else 1 if item["retrieval_method"] == "exact_phrase" else 2
            ),
            item["_score"],
            item["page_number"],
            item["attachment_id"],
        ),
    )[:limit]
    results = []
    for rank, row in enumerate(ordered, 1):
        full_text = row.pop("text", "")
        terms = row.pop("_matched_terms", primary_terms + bridge_terms)
        row.pop("_score", None)
        snippet, start, end, matched_terms = _services()._document_search_snippet(
            full_text, terms, query
        )
        row["snippet"] = snippet
        row["matched_terms"] = matched_terms
        row["lexical_rank"] = rank
        row["evidence_anchor"] = {
            "attachment_id": row["attachment_id"],
            "page_number": row["page_number"],
            "start_char": start,
            "end_char": end,
            "text_sha256": row["text_sha256"],
        }
        results.append(row)
    return results


def _embedding_index_model(settings: dict | None = None) -> str:
    settings = settings or _services().model_settings()
    model = _services().clean_text(settings.get("embedding_model", ""), 200)
    endpoint = (
        str(
            settings.get("effective_embedding_api_url")
            or settings.get("embedding_api_url", "")
        )
        .strip()
        .rstrip("/")
    )
    if not model or not endpoint:
        return ""
    identity = "\n".join((settings.get("provider", ""), endpoint, model))
    return _services().hashlib.sha256(identity.encode("utf-8")).hexdigest()


def request_embeddings(
    texts: list[str], settings: dict | None = None, api_key: str | None = None
) -> tuple[list[list[float]], int]:
    """Call one configured OpenAI-compatible Embeddings endpoint."""
    settings = settings or _services().model_settings()
    model = _services().clean_text(settings.get("embedding_model", ""), 200)
    endpoint = (
        str(
            settings.get("effective_embedding_api_url")
            or settings.get("embedding_api_url", "")
        )
        .strip()
        .rstrip("/")
    )
    api_key = _services().model_api_key() if api_key is None else str(api_key).strip()
    if not model or not endpoint:
        raise ValueError("尚未配置 Embedding 模型和 API 地址")
    if not api_key:
        raise ValueError("尚未配置模型 API Key")
    values = [_services().clean_embedding_text(item) for item in texts]
    if not values or any(not value for value in values):
        raise ValueError("Embedding 输入不能为空")
    payload = {"model": model, "input": values}

    def send():
        with _services().model_usage_context("embedding", "embedding"):
            response = _services().open_compatible_model_request(
                endpoint, payload, api_key, 90
            )
            metadata = getattr(response, "_litloom_model_usage", None)
            result = None
            error = None
            try:
                with response:
                    result = _services().json.loads(
                        response.read().decode("utf-8", "replace")
                    )
                return result
            except Exception as exc:
                error = exc
                raise
            finally:
                if metadata:
                    _services().record_model_usage(metadata, result=result, error=error)

    for attempt in range(2):
        try:
            result = _services()._with_model_request_budget(send)
            return _services().validate_embedding_response(result, len(values))
        except (_services().json.JSONDecodeError, UnicodeDecodeError, ValueError):
            if attempt:
                raise
            _services().time.sleep(0.25)
    raise RuntimeError("Embedding 请求重试次数已用尽")


def semantic_index_job_snapshot(row) -> dict:
    if not row:
        return {}
    item = dict(row)
    total = max(0, int(item.get("progress_total") or 0))
    current = max(0, int(item.get("progress_current") or 0))
    item["progress_percent"] = (
        round(current * 100 / total)
        if total
        else (100 if item.get("status") in {"success", "partial"} else 0)
    )
    item["cancel_requested"] = bool(item.get("cancel_requested"))
    item["duration_ms"] = 0
    try:
        started = _services().datetime.fromisoformat(
            item.get("started_at") or item.get("created_at")
        )
        ended = _services().datetime.fromisoformat(
            item.get("completed_at") or _services().now_iso()
        )
        item["duration_ms"] = max(0, round((ended - started).total_seconds() * 1000, 2))
    except (TypeError, ValueError):
        pass
    return item


def get_semantic_index_job(job_id: str) -> dict | None:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT * FROM semantic_index_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return _services().semantic_index_job_snapshot(row) if row else None


def get_semantic_index_status(paper_id: str) -> dict:
    settings = _services().model_settings()
    index_model = _services()._embedding_index_model(settings)
    with _services().DB_LOCK, _services().db_session() as db:
        paper = db.execute("SELECT id FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not paper:
            raise LookupError("论文不存在")
        pages = (
            db.execute(
                """SELECT dp.id,dp.text_sha256,COUNT(e.page_id) AS indexed_chunks,
                      MIN(e.text_sha256) AS indexed_hash_min,MAX(e.text_sha256) AS indexed_hash_max,
                      MAX(e.chunk_count) AS expected_chunks
               FROM document_pages dp
               LEFT JOIN document_page_embeddings e ON e.page_id=dp.id AND e.model=?
               WHERE dp.paper_id=? AND dp.quality<>'empty' AND length(trim(dp.text))>0
               GROUP BY dp.id ORDER BY dp.id""",
                (index_model, paper_id),
            ).fetchall()
            if index_model
            else []
        )
        page_total = db.execute(
            "SELECT COUNT(*) FROM document_pages WHERE paper_id=? AND quality<>'empty' AND length(trim(text))>0",
            (paper_id,),
        ).fetchone()[0]
        latest = db.execute(
            "SELECT * FROM semantic_index_jobs WHERE paper_id=? ORDER BY created_at DESC LIMIT 1",
            (paper_id,),
        ).fetchone()
        active = db.execute(
            "SELECT * FROM semantic_index_jobs WHERE paper_id=? AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
            (paper_id,),
        ).fetchone()
        indexed_pages = sum(
            1
            for row in pages
            if row["indexed_chunks"]
            and row["indexed_chunks"] == row["expected_chunks"]
            and row["indexed_hash_min"] == row["text_sha256"] == row["indexed_hash_max"]
        )
        chunk_count = (
            db.execute(
                "SELECT COUNT(*) FROM document_page_embeddings e JOIN document_pages dp ON dp.id=e.page_id WHERE e.paper_id=? AND e.model=? AND e.text_sha256=dp.text_sha256",
                (paper_id, index_model),
            ).fetchone()[0]
            if index_model
            else 0
        )
    configured = bool(
        settings.get("embedding_configured") and settings.get("api_key_set")
    )
    return {
        "paper_id": paper_id,
        "configured": configured,
        "embedding_model": settings.get("embedding_model", ""),
        "index_model": index_model,
        "page_count": int(page_total),
        "indexed_pages": int(indexed_pages),
        "stale_pages": max(0, int(page_total) - int(indexed_pages)),
        "indexed_chunks": int(chunk_count),
        "ready": bool(configured and indexed_pages),
        "latest_job": _services().semantic_index_job_snapshot(latest),
        "active_job": _services().semantic_index_job_snapshot(active),
    }


def _semantic_job_cancelled(job_id: str) -> bool:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT cancel_requested FROM semantic_index_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return not row or bool(row["cancel_requested"])


def _update_semantic_index_job(job_id: str, **updates) -> dict:
    allowed = {
        "status",
        "stage",
        "progress_current",
        "progress_total",
        "failed_pages",
        "message",
        "error",
        "cancel_requested",
        "started_at",
        "updated_at",
        "completed_at",
    }
    values = {key: value for key, value in updates.items() if key in allowed}
    values.setdefault("updated_at", _services().now_iso())
    assignments = ",".join(f"{key}=?" for key in values)
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            f"UPDATE semantic_index_jobs SET {assignments} WHERE id=?",
            (*values.values(), job_id),
        )
        row = db.execute(
            "SELECT * FROM semantic_index_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return _services().semantic_index_job_snapshot(row) if row else {}


def run_semantic_index_job(job_id: str) -> None:
    with _services().DB_LOCK, _services().db_session() as db:
        job = db.execute(
            "SELECT * FROM semantic_index_jobs WHERE id=?", (job_id,)
        ).fetchone()
    if not job or job["status"] != "queued":
        return
    settings = _services().model_settings()
    index_model = _services()._embedding_index_model(settings)
    if not index_model or index_model != job["model"]:
        _services()._update_semantic_index_job(
            job_id,
            status="failed",
            stage="failed",
            message="Embedding 配置已变化",
            error="任务创建后 Embedding 模型或接口已变化，请重新启动索引",
            completed_at=_services().now_iso(),
        )
        return
    job_api_key = _services().model_api_key()
    if not job_api_key:
        _services()._update_semantic_index_job(
            job_id,
            status="failed",
            stage="failed",
            message="缺少模型 API Key",
            error="请先配置模型 API Key，然后重试",
            completed_at=_services().now_iso(),
        )
        return
    with _services().DB_LOCK, _services().db_session() as db:
        total = int(
            db.execute(
                "SELECT COUNT(*) FROM document_pages WHERE paper_id=? AND quality<>'empty' AND length(trim(text))>0",
                (job["paper_id"],),
            ).fetchone()[0]
        )
        existing_dimensions = {
            int(row[0])
            for row in db.execute(
                "SELECT DISTINCT dimensions FROM document_page_embeddings WHERE model=? LIMIT 2",
                (index_model,),
            ).fetchall()
        }
    if len(existing_dimensions) > 1:
        _services()._update_semantic_index_job(
            job_id,
            status="failed",
            stage="failed",
            message="现有向量维度不一致",
            error="请更换 Embedding 模型 ID 后重新建立索引",
            completed_at=_services().now_iso(),
        )
        return
    expected_dimension = next(iter(existing_dimensions), 0)
    _services()._update_semantic_index_job(
        job_id,
        status="running",
        stage="indexing",
        progress_current=0,
        progress_total=total,
        failed_pages=0,
        message="准备按页构建语义证据索引",
        started_at=_services().now_iso(),
        completed_at="",
        error="",
    )
    last_page_id = 0
    processed_pages = 0
    failed_pages = 0
    failure_messages = []
    try:
        while not _services().STOP_EVENT.is_set():
            if _services()._semantic_job_cancelled(job_id):
                _services()._update_semantic_index_job(
                    job_id,
                    status="cancelled",
                    stage="cancelled",
                    message="语义索引已取消；已完成页面保留",
                    completed_at=_services().now_iso(),
                )
                return
            with _services().DB_LOCK, _services().db_session() as db:
                page_rows = db.execute(
                    "SELECT id,paper_id,attachment_id,page_number,text,text_sha256 FROM document_pages WHERE paper_id=? AND id>? AND quality<>'empty' AND length(trim(text))>0 ORDER BY id LIMIT 8",
                    (job["paper_id"], last_page_id),
                ).fetchall()
            if not page_rows:
                break
            pages = []
            entries = []
            for row in page_rows:
                page = dict(row)
                last_page_id = page["id"]
                chunks = _services().split_text(page["text"])
                if not chunks:
                    continue
                with _services().db_session() as db:
                    existing = db.execute(
                        "SELECT COUNT(*) AS n,MIN(text_sha256) AS lo,MAX(text_sha256) AS hi,MAX(chunk_count) AS total FROM document_page_embeddings WHERE page_id=? AND model=?",
                        (page["id"], index_model),
                    ).fetchone()
                if (
                    not job["force_rebuild"]
                    and existing["n"] == len(chunks)
                    and existing["total"] == len(chunks)
                    and existing["lo"] == page["text_sha256"] == existing["hi"]
                ):
                    processed_pages += 1
                    continue
                page["chunks"] = chunks
                page["vectors"] = [None] * len(chunks)
                page["failed"] = False
                pages.append(page)
                for chunk_index, (text, start, end) in enumerate(chunks):
                    entries.append(
                        {
                            "page": page,
                            "chunk_index": chunk_index,
                            "text": text,
                            "start": start,
                            "end": end,
                        }
                    )

            for offset in range(0, len(entries), 32):
                if (
                    _services().STOP_EVENT.is_set()
                    or _services()._semantic_job_cancelled(job_id)
                ):
                    if _services()._semantic_job_cancelled(job_id):
                        _services()._update_semantic_index_job(
                            job_id,
                            status="cancelled",
                            stage="cancelled",
                            progress_current=processed_pages,
                            message="语义索引已取消；已完成页面保留",
                            completed_at=_services().now_iso(),
                        )
                        return
                    break
                batch = entries[offset : offset + 32]
                if (
                    _services()._embedding_index_model(_services().model_settings())
                    != index_model
                ):
                    raise RuntimeError(
                        "Embedding 配置在任务运行期间发生变化；为避免将密钥发往旧接口，索引已停止"
                    )
                try:
                    vectors, dimensions = _services().request_embeddings(
                        [item["text"] for item in batch], settings, job_api_key
                    )
                    if expected_dimension and expected_dimension != dimensions:
                        raise ValueError(
                            "Embedding 模型返回维度与已有索引不一致；请使用新模型 ID 后重建"
                        )
                    expected_dimension = dimensions
                    for item, vector in zip(batch, vectors):
                        item["page"]["vectors"][item["chunk_index"]] = vector
                except Exception as exc:
                    message = _services().clean_text(
                        _services().model_error_detail(exc, job_api_key), 400
                    )
                    for item in batch:
                        item["page"]["failed"] = True
                    failure_messages.append(message or "Embedding 请求失败")

            now = _services().now_iso()
            with _services().DB_LOCK, _services().db_session() as db:
                for page in pages:
                    if page["failed"] or any(
                        vector is None for vector in page["vectors"]
                    ):
                        failed_pages += 1
                        continue
                    db.execute(
                        "DELETE FROM document_page_embeddings WHERE page_id=? AND model=?",
                        (page["id"], index_model),
                    )
                    rows = [
                        (
                            page["id"],
                            page["paper_id"],
                            chunk_index,
                            page["text_sha256"],
                            index_model,
                            len(vector),
                            len(page["chunks"]),
                            page["chunks"][chunk_index][1],
                            page["chunks"][chunk_index][2],
                            _services().json.dumps(vector, separators=(",", ":")),
                            now,
                        )
                        for chunk_index, vector in enumerate(page["vectors"])
                    ]
                    db.executemany(
                        "INSERT INTO document_page_embeddings(page_id,paper_id,chunk_index,text_sha256,model,dimensions,chunk_count,start_char,end_char,vector_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        rows,
                    )
            processed_pages += len(page_rows)
            _services()._update_semantic_index_job(
                job_id,
                progress_current=min(processed_pages, total),
                failed_pages=failed_pages,
                message=f"已处理 {min(processed_pages, total)}/{total} 页 · 失败 {failed_pages} 页",
            )
        if _services().STOP_EVENT.is_set():
            _services()._update_semantic_index_job(
                job_id,
                status="interrupted",
                stage="interrupted",
                progress_current=min(processed_pages, total),
                failed_pages=failed_pages,
                message="服务停止，索引将在下次启动后续跑",
                error="服务重启时任务未完成",
                completed_at=_services().now_iso(),
            )
            return
        status = "partial" if failed_pages else "success"
        detail = "; ".join(dict.fromkeys(failure_messages))[:700]
        _services()._update_semantic_index_job(
            job_id,
            status=status,
            stage="completed",
            progress_current=min(processed_pages, total),
            failed_pages=failed_pages,
            message=f"语义索引完成：{processed_pages - failed_pages}/{total} 页可用",
            error=detail,
            completed_at=_services().now_iso(),
        )
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "DELETE FROM document_page_embeddings WHERE paper_id=? AND model<>?",
                (job["paper_id"], index_model),
            )
            _services().record_audit(
                db,
                (
                    "paper.semantic_index.completed"
                    if status == "success"
                    else "paper.semantic_index.partial"
                ),
                "paper",
                job["paper_id"],
                {
                    "model": settings.get("embedding_model", ""),
                    "indexed_pages": max(0, processed_pages - failed_pages),
                    "failed_pages": failed_pages,
                },
            )
    except Exception as exc:
        detail = _services().clean_text(
            _services().model_error_detail(exc, job_api_key), 700
        )
        _services()._update_semantic_index_job(
            job_id,
            status="failed",
            stage="failed",
            progress_current=min(processed_pages, total),
            failed_pages=failed_pages,
            message="语义索引失败，可重试",
            error=detail,
            completed_at=_services().now_iso(),
        )


def _enqueue_semantic_index_job(job_id: str) -> None:
    if _services().STOP_EVENT.is_set():
        _services()._update_semantic_index_job(
            job_id,
            status="interrupted",
            stage="interrupted",
            message=_services().RUNTIME_SHUTDOWN_MESSAGE,
            error="服务重启时任务未完成",
            completed_at=_services().now_iso(),
        )
        raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
    with _services().SEMANTIC_INDEX_EXECUTOR_LOCK:
        if _services().STOP_EVENT.is_set():
            _services()._update_semantic_index_job(
                job_id,
                status="interrupted",
                stage="interrupted",
                message=_services().RUNTIME_SHUTDOWN_MESSAGE,
                error="服务重启时任务未完成",
                completed_at=_services().now_iso(),
            )
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
        if _services().SEMANTIC_INDEX_EXECUTOR is None:
            _services().SEMANTIC_INDEX_EXECUTOR = _services().ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="semantic-index"
            )
        executor = _services().SEMANTIC_INDEX_EXECUTOR
        try:
            executor.submit(_services().run_semantic_index_job, job_id)
        except RuntimeError as exc:
            if _services().SEMANTIC_INDEX_EXECUTOR is executor:
                _services().SEMANTIC_INDEX_EXECUTOR = None
            _services()._update_semantic_index_job(
                job_id,
                status="interrupted",
                stage="interrupted",
                message=_services().RUNTIME_SHUTDOWN_MESSAGE,
                error="服务重启时任务未完成",
                completed_at=_services().now_iso(),
            )
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE) from exc


def submit_semantic_index_job(
    paper_id: str, force: bool = False, consent: bool = False
) -> dict:
    if consent is not True:
        raise PermissionError(
            "建立语义索引会将该论文已提取的文字发送到 Embedding 服务；请先确认本次授权"
        )
    settings = _services().model_settings()
    index_model = _services()._embedding_index_model(settings)
    if not settings.get("embedding_model") or not settings.get(
        "effective_embedding_api_url"
    ):
        raise ValueError(
            "请先在模型配置中心填写 Embedding 模型 ID；API 地址可留空并按 Base URL 自动推导"
        )
    if not settings.get("api_key_set"):
        raise ValueError("请先配置模型 API Key")
    status = _services().get_semantic_index_status(paper_id)
    if not force and status["stale_pages"] == 0:
        return {"up_to_date": True, "status": status}
    with _services().DB_LOCK, _services().db_session() as db:
        active = db.execute(
            "SELECT * FROM semantic_index_jobs WHERE paper_id=? AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
            (paper_id,),
        ).fetchone()
        if active:
            return {
                "job": _services().semantic_index_job_snapshot(active),
                "deduplicated": True,
            }
        job_id = str(_services().uuid.uuid4())
        created = _services().now_iso()
        db.execute(
            "INSERT INTO semantic_index_jobs(id,paper_id,model,status,stage,force_rebuild,progress_total,message,created_at,updated_at) VALUES(?,?,?,'queued','queued',?,?,?,?,?)",
            (
                job_id,
                paper_id,
                index_model,
                int(bool(force)),
                status["page_count"],
                "已加入语义索引队列",
                created,
                created,
            ),
        )
        row = db.execute(
            "SELECT * FROM semantic_index_jobs WHERE id=?", (job_id,)
        ).fetchone()
    _services()._enqueue_semantic_index_job(job_id)
    return {"job": _services().semantic_index_job_snapshot(row), "deduplicated": False}


def cancel_semantic_index_job(job_id: str) -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT * FROM semantic_index_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row:
            raise LookupError("语义索引任务不存在")
        if row["status"] == "queued":
            db.execute(
                "UPDATE semantic_index_jobs SET status='cancelled',stage='cancelled',message='语义索引已取消',updated_at=?,completed_at=? WHERE id=?",
                (_services().now_iso(), _services().now_iso(), job_id),
            )
        elif row["status"] == "running":
            db.execute(
                "UPDATE semantic_index_jobs SET cancel_requested=1,message='将在当前 Embedding 请求结束后取消',updated_at=? WHERE id=?",
                (_services().now_iso(), job_id),
            )
        updated = db.execute(
            "SELECT * FROM semantic_index_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return _services().semantic_index_job_snapshot(updated)


def resume_semantic_index_jobs() -> None:
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT id FROM semantic_index_jobs WHERE status='interrupted' AND error='服务重启时任务未完成' ORDER BY created_at"
        ).fetchall()
        db.execute(
            "UPDATE semantic_index_jobs SET status='queued',stage='queued',message='服务启动后继续处理索引',error='',started_at='',updated_at=? WHERE status='interrupted' AND error='服务重启时任务未完成'",
            (_services().now_iso(),),
        )
    for row in rows:
        try:
            _services()._enqueue_semantic_index_job(row["id"])
        except RuntimeError:
            continue


def search_document_pages_detailed(
    paper_id: str, query: str, limit: int = 20, semantic: bool = False
) -> dict:
    lexical = _services().search_document_pages(paper_id, query, limit)
    if not semantic:
        return {"pages": lexical, "retrieval_mode": "bm25", "retrieval_warning": ""}
    status = _services().get_semantic_index_status(paper_id)
    if not status["configured"]:
        return {
            "pages": lexical,
            "retrieval_mode": "bm25_fallback",
            "retrieval_warning": "尚未配置 Embedding 模型或 API Key，已使用 BM25 词法检索。",
        }
    if not status["indexed_chunks"]:
        return {
            "pages": lexical,
            "retrieval_mode": "bm25_fallback",
            "retrieval_warning": "该论文尚无可用语义索引；请先建立索引，当前已使用 BM25。",
        }
    settings = _services().model_settings()
    try:
        query_vectors, query_dimensions = _services().request_embeddings(
            [_services().clean_text(query, 300)], settings
        )
        terms, bridge = _services()._document_search_terms(query)
        best_by_page = {}
        with _services().db_session() as db:
            cursor = db.execute(
                """SELECT dp.attachment_id,e.page_id,e.chunk_index,e.dimensions,e.start_char,e.end_char,e.vector_json,
                          dp.page_number,dp.text,dp.char_count,dp.quality,dp.extractor,dp.text_sha256,dp.extracted_at
                   FROM document_page_embeddings e JOIN document_pages dp ON dp.id=e.page_id
                   WHERE e.paper_id=? AND e.model=? AND e.text_sha256=dp.text_sha256
                   ORDER BY dp.page_number,e.chunk_index""",
                (paper_id, status["index_model"]),
            )
            while True:
                with _services().DB_LOCK:
                    rows = cursor.fetchmany(256)
                if not rows:
                    break
                for row in rows:
                    if int(row["dimensions"]) != query_dimensions:
                        continue
                    try:
                        vector = _services().json.loads(row["vector_json"])
                    except (TypeError, ValueError, _services().json.JSONDecodeError):
                        continue
                    score = _services().cosine_similarity(query_vectors[0], vector)
                    key = (row["attachment_id"], int(row["page_number"]))
                    previous = best_by_page.get(key)
                    if previous and previous["semantic_score"] >= score:
                        continue
                    text = row["text"] or ""
                    start = max(0, min(len(text), int(row["start_char"])))
                    end = max(start, min(len(text), int(row["end_char"])))
                    chunk = text[start:end]
                    (
                        snippet,
                        local_start,
                        local_end,
                        matched_terms,
                    ) = _services()._document_search_snippet(
                        chunk, terms + bridge, query
                    )
                    item = {
                        key: row[key]
                        for key in (
                            "attachment_id",
                            "page_number",
                            "char_count",
                            "quality",
                            "extractor",
                            "text_sha256",
                            "extracted_at",
                        )
                    }
                    item.update(
                        {
                            "snippet": snippet,
                            "matched_terms": matched_terms,
                            "retrieval_method": "semantic",
                            "semantic_score": round(score, 6),
                            "semantic_chunk_index": int(row["chunk_index"]),
                            "evidence_anchor": {
                                "attachment_id": row["attachment_id"],
                                "page_number": int(row["page_number"]),
                                "start_char": start + local_start,
                                "end_char": min(end, start + local_end),
                                "text_sha256": row["text_sha256"],
                            },
                        }
                    )
                    best_by_page[key] = item
        semantic_rows = sorted(
            best_by_page.values(),
            key=lambda item: (
                -item["semantic_score"],
                item["page_number"],
                item["attachment_id"],
            ),
        )[: max(1, min(limit, 100))]
        if not semantic_rows:
            return {
                "pages": lexical,
                "retrieval_mode": "bm25_fallback",
                "retrieval_warning": "语义索引与查询向量维度不匹配或没有有效结果，已回退到 BM25。",
            }
        pages = _services().reciprocal_rank_fusion(lexical, semantic_rows)[
            : max(1, min(int(limit), 100))
        ]
        for rank, item in enumerate(pages, 1):
            item["hybrid_rank"] = rank
        warning = (
            "语义索引尚未覆盖全部可检索页面，结果可能不完整。"
            if status["stale_pages"]
            else ""
        )
        return {
            "pages": pages,
            "retrieval_mode": "hybrid",
            "retrieval_warning": warning,
            "semantic_model": settings.get("embedding_model", ""),
        }
    except Exception as exc:
        warning = _services().clean_text(
            _services().model_error_detail(exc, _services().model_api_key()), 300
        )
        return {
            "pages": lexical,
            "retrieval_mode": "bm25_fallback",
            "retrieval_warning": f"语义检索暂不可用，已回退到 BM25：{warning}",
        }


__all__ = [
    "extract_attachment_pages",
    "extract_paper_documents",
    "extraction_summary",
    "store_pdf_attachment",
    "pdf_local_metadata",
    "_fill_paper_metadata",
    "crossref_metadata",
    "complete_paper_metadata_draft",
    "_metadata_draft_input",
    "metadata_draft_job_snapshot",
    "get_metadata_draft_job",
    "_update_metadata_draft_job",
    "run_metadata_draft_job",
    "_mark_metadata_draft_interrupted_for_shutdown",
    "_enqueue_metadata_draft_job",
    "submit_metadata_draft_job",
    "retry_metadata_draft_job",
    "resume_metadata_draft_jobs",
    "_ingest_pdf_item",
    "_run_pdf_ingest_job",
    "_mark_pdf_ingest_interrupted_for_shutdown",
    "_enqueue_pdf_ingest_job",
    "submit_pdf_ingest_job",
    "resume_pdf_ingest_jobs",
    "create_pdf_ingest_job",
    "pdf_ingest_job_snapshot",
    "list_pdf_ingest_jobs",
    "register_paper_folder",
    "retry_pdf_ingest_job",
    "scan_paper_folder",
    "_document_search_terms",
    "_document_fts_expression",
    "_document_casefold_span",
    "_document_search_snippet",
    "search_document_pages",
    "_embedding_index_model",
    "request_embeddings",
    "semantic_index_job_snapshot",
    "get_semantic_index_job",
    "get_semantic_index_status",
    "_semantic_job_cancelled",
    "_update_semantic_index_job",
    "run_semantic_index_job",
    "_enqueue_semantic_index_job",
    "submit_semantic_index_job",
    "cancel_semantic_index_job",
    "resume_semantic_index_jobs",
    "search_document_pages_detailed",
]
