"""PDF attachment, extraction, ingestion, and semantic indexing."""

from __future__ import annotations
from pathlib import Path

from .services import app_services as _services


def extract_attachment_pages(attachment_id: str) -> dict:
    """Extract page text as a rebuildable, quality-labelled derived artifact."""
    try:
        from pypdf import PdfReader

        extractor = "pypdf"
    except ImportError:
        try:
            from PyPDF2 import PdfReader

            extractor = "PyPDF2"
        except ImportError as exc:
            raise ValueError(
                "PDF 文本提取需要可选依赖 pypdf；请运行 pip install pypdf"
            ) from exc
    with _services().DB_LOCK, _services().db_session() as db:
        attachment = db.execute(
            "SELECT id,paper_id,stored_name,original_name FROM attachments WHERE id=?",
            (attachment_id,),
        ).fetchone()
    if not attachment:
        raise ValueError("附件不存在")
    target = (
        _services().active_files_dir()
        / _services().Path(attachment["stored_name"]).name
    )
    if not target.is_file():
        raise ValueError("附件文件不存在，可能需要从备份恢复")
    try:
        reader = PdfReader(str(target))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ValueError("PDF 已加密，无法在没有密码的情况下提取文本") from exc
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            raw = page.extract_text() or ""
            text = _services().clean_text(raw.replace("\x00", ""), 500_000)
            chars = len(text.strip())
            quality = "good" if chars >= 160 else ("low" if chars else "empty")
            pages.append(
                {
                    "page_number": index,
                    "text": text,
                    "char_count": chars,
                    "quality": quality,
                    "text_sha256": _services()
                    .hashlib.sha256(text.encode("utf-8"))
                    .hexdigest(),
                }
            )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"PDF 解析失败：{str(exc)[:300]}") from exc
    extracted_at = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute("DELETE FROM document_pages WHERE attachment_id=?", (attachment_id,))
        for item in pages:
            db.execute(
                "INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    attachment_id,
                    attachment["paper_id"],
                    item["page_number"],
                    item["text"],
                    item["char_count"],
                    item["quality"],
                    extractor,
                    item["text_sha256"],
                    extracted_at,
                ),
            )
        _services().record_audit(
            db,
            "document.extracted",
            "attachment",
            attachment_id,
            {
                "paper_id": attachment["paper_id"],
                "pages": len(pages),
                "nonempty_pages": sum(item["quality"] != "empty" for item in pages),
                "extractor": extractor,
            },
        )
    return {
        "attachment_id": attachment_id,
        "paper_id": attachment["paper_id"],
        "filename": attachment["original_name"],
        "page_count": len(pages),
        "nonempty_pages": sum(item["quality"] != "empty" for item in pages),
        "characters": sum(item["char_count"] for item in pages),
        "extractor": extractor,
        "extracted_at": extracted_at,
    }


def extract_paper_documents(paper_id: str) -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        attachments = [
            dict(row)
            for row in db.execute(
                "SELECT id FROM attachments WHERE paper_id=? AND id IN ("
                "SELECT MIN(id) FROM attachments WHERE paper_id=? GROUP BY CASE WHEN sha256<>'' THEN sha256 ELSE id END"
                ") ORDER BY added_at",
                (paper_id, paper_id),
            )
        ]
    if not attachments:
        raise ValueError("这篇论文还没有本地 PDF 附件")
    # Attachments are independent files.  Parse them concurrently, while each
    # worker keeps its SQLite writes behind DB_LOCK in extract_attachment_pages.
    # The future list preserves attachment order in the returned artifact.
    worker_count = min(3, max(1, len(attachments)))
    with _services().ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="paper-pdf-extract"
    ) as pool:
        futures = [
            pool.submit(_services().extract_attachment_pages, item["id"])
            for item in attachments
        ]
        results = [future.result() for future in futures]
    return {
        "paper_id": paper_id,
        "attachments": results,
        "page_count": sum(item["page_count"] for item in results),
        "characters": sum(item["characters"] for item in results),
        "extracted_at": max(item["extracted_at"] for item in results),
    }


def extraction_summary(paper_id: str) -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT attachment_id,MAX(extracted_at) AS extracted_at,COUNT(*) AS page_count,SUM(char_count) AS characters,SUM(quality='good') AS good_pages,SUM(quality='low') AS low_pages,SUM(quality='empty') AS empty_pages,MAX(extractor) AS extractor FROM document_pages WHERE paper_id=? GROUP BY attachment_id",
                (paper_id,),
            )
        ]
        attachments = db.execute(
            "SELECT COUNT(*) FROM attachments WHERE paper_id=?", (paper_id,)
        ).fetchone()[0]
    return {
        "paper_id": paper_id,
        "attachment_count": attachments,
        "extracted_attachment_count": len(rows),
        "pages": rows,
        "page_count": sum(int(row["page_count"] or 0) for row in rows),
        "characters": sum(int(row["characters"] or 0) for row in rows),
    }


def store_pdf_attachment(
    paper_id: str, filename: str, content: bytes | str | Path
) -> tuple[dict, bool]:
    """Store a validated PDF in the content-addressed attachment store."""
    filename = (
        _services().Path(_services().clean_text(filename, 250)).name or "paper.pdf"
    )
    if not filename.lower().endswith(".pdf"):
        raise ValueError("当前仅支持 PDF 文件")
    source_path = (
        _services().Path(content)
        if isinstance(content, (str, _services().Path))
        else None
    )
    if source_path:
        if not source_path.is_file():
            raise ValueError("PDF 文件不存在或不可访问")
        byte_size = source_path.stat().st_size
        with source_path.open("rb") as handle:
            is_pdf = handle.read(4).startswith(b"%PDF")
    else:
        byte_size = len(content)
        is_pdf = content.startswith(b"%PDF")
    if byte_size > 100 * 1024 * 1024:
        raise ValueError("单个 PDF 限制为 100 MB")
    if not is_pdf:
        raise ValueError("文件内容不像有效 PDF")
    with _services().DB_LOCK, _services().db_session() as db:
        if not db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
            raise ValueError("论文不存在")
    digest = (
        _services().sha256_file(source_path)
        if source_path
        else _services().hashlib.sha256(content).hexdigest()
    )
    _services().active_files_dir().mkdir(parents=True, exist_ok=True)
    stored = digest + ".pdf"
    target = _services().active_files_dir() / stored
    if not target.exists():
        if source_path:
            import shutil

            shutil.copyfile(source_path, target)
        else:
            target.write_bytes(content)
    with _services().DB_LOCK, _services().db_session() as db:
        existing = db.execute(
            "SELECT id,original_name,stored_name,sha256,byte_size FROM attachments WHERE paper_id=? AND sha256=? ORDER BY added_at LIMIT 1",
            (paper_id, digest),
        ).fetchone()
        if existing:
            return {
                "id": existing["id"],
                "filename": existing["original_name"],
                "sha256": existing["sha256"],
                "url": "/files/" + existing["stored_name"],
                "deduplicated": True,
            }, True
        aid = str(_services().uuid.uuid4())
        db.execute(
            "INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)",
            (aid, paper_id, filename, stored, digest, byte_size, _services().now_iso()),
        )
        _services().record_audit(
            db,
            "attachment.added",
            "paper",
            paper_id,
            {"sha256": digest, "byte_size": byte_size, "ingest": True},
        )
    return {
        "id": aid,
        "filename": filename,
        "sha256": digest,
        "url": "/files/" + stored,
        "deduplicated": False,
    }, False


def pdf_local_metadata(source, filename: str) -> tuple[dict, str]:
    """Extract cheap, deterministic metadata before any model request."""
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader
    try:
        reader = (
            PdfReader(str(source))
            if isinstance(source, (str, _services().Path))
            else PdfReader(_services().io.BytesIO(source))
        )
        pages = []
        for index, page in enumerate(reader.pages):
            if index >= 3:
                break
            pages.append(
                _services().clean_text(
                    (page.extract_text() or "").replace("\x00", ""), 30000
                )
            )
        preview = "\n\n".join(item for item in pages if item)
        info = reader.metadata or {}
    except Exception as exc:
        raise ValueError(f"PDF 解析失败：{str(exc)[:240]}") from exc
    name = _services().Path(filename).stem
    doi_match = _services().re.search(
        r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", preview + " " + name, _services().re.I
    )
    arxiv_match = _services().re.search(
        r"(?:arxiv[: _/-]*)?(\d{4}\.\d{4,5}(?:v\d+)?)",
        preview + " " + name,
        _services().re.I,
    )
    year_match = _services().re.search(
        r"\b((?:19|20)\d{2})\b", preview[:6000] + " " + name
    )
    title = _services().clean_text(info.get("/Title", "") if info else "", 1000)
    if title.casefold().strip(" ._-") in {
        "untitled",
        "unknown",
        "document",
        "microsoft word",
    }:
        title = ""
    # First non-trivial line is a better fallback than a cryptic filename.
    lines = [
        _services().re.sub(r"\s+", " ", line).strip(" ·|\t")
        for line in preview.splitlines()
    ]
    candidates = [
        line
        for line in lines[:35]
        if 15 <= len(line) <= 240 and not _services().re.fullmatch(r"[\d\W]+", line)
    ]
    if not title and candidates:
        title = candidates[0]
    if not title:
        title = _services().re.sub(r"[_-]+", " ", name).strip() or "未命名论文"
    authors = _services().clean_text(info.get("/Author", "") if info else "", 4000)
    if authors.casefold().strip(" ._-") in {"anonymous", "unknown", "author"}:
        authors = ""
    metadata = {
        "title": title,
        "authors": authors,
        "year": int(year_match.group(1)) if year_match else None,
        "doi": _services().norm_doi(doi_match.group(0)) if doi_match else "",
        "arxiv_id": _services().norm_arxiv(arxiv_match.group(1)) if arxiv_match else "",
        "abstract": "",
        "venue": "",
        "url": "",
        "pdf_url": "",
    }
    return metadata, preview


def _fill_paper_metadata(
    paper_id: str, record: dict, source: str, replace_title: bool = False
) -> None:
    """Fill only blank bibliographic fields; never overwrite user edits."""
    fields = (
        "title",
        "authors",
        "year",
        "abstract",
        "doi",
        "arxiv_id",
        "venue",
        "url",
        "pdf_url",
    )
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row:
            return
        updates = []
        values = []
        for field in fields:
            value = record.get(field)
            if value not in (None, "", []) and (
                not row[field] or (field == "title" and replace_title)
            ):
                updates.append(f"{field}=?")
                values.append(value)
        if "title" in [item.split("=")[0] for item in updates]:
            updates.append("title_key=?")
            values.append(
                _services().title_key(_services().clean_text(record.get("title"), 1000))
            )
        if updates:
            values.extend([_services().now_iso(), paper_id])
            db.execute(
                f"UPDATE papers SET {','.join(updates)},updated_at=? WHERE id=?", values
            )
            _services().refresh_search_content(db, paper_id)
            _services().record_audit(
                db,
                "paper.metadata_filled",
                "paper",
                paper_id,
                {
                    "source": source,
                    "fields": [
                        field
                        for field in fields
                        if record.get(field) not in (None, "", [])
                        and (not row[field] or (field == "title" and replace_title))
                    ],
                },
            )


def crossref_metadata(doi: str) -> dict:
    doi = _services().norm_doi(doi)
    if not doi:
        return {}
    try:
        result = _services().http_json(
            "https://api.crossref.org/works/" + _services().quote(doi, safe=""),
            timeout=7,
        )
        work = result.get("message") or {}
        titles = work.get("title") or []
        authors = work.get("author") or []
        date = (
            work.get("published-print")
            or work.get("published-online")
            or work.get("created")
            or {}
        )
        year = ((date.get("date-parts") or [[None]])[0] or [None])[0]
        abstract = _services().re.sub(r"<[^>]+>", " ", str(work.get("abstract") or ""))
        return {
            "title": _services().clean_text(titles[0] if titles else "", 1000),
            "authors": ", ".join(
                _services().clean_text(
                    " ".join(
                        part for part in (item.get("given"), item.get("family")) if part
                    ),
                    200,
                )
                for item in authors[:20]
            ),
            "year": year,
            "doi": doi,
            "abstract": _services().clean_text(abstract, 30000),
            "venue": _services().clean_text(
                (work.get("container-title") or [""])[0], 1000
            ),
            "url": _services().clean_text(work.get("URL", ""), 3000),
        }
    except Exception:
        return {}


def complete_paper_metadata_draft(data: dict, progress_callback=None) -> dict:
    """Build a reviewable metadata draft from DOI/title lookup and optional AI extraction."""
    title = _services().clean_text(data.get("title", ""), 1000)
    citation = _services().clean_text(data.get("citation", ""), 20000)
    doi = _services().norm_doi(_services().clean_text(data.get("doi", ""), 300))
    if progress_callback:
        progress_callback("crossref", 0, 2, "正在查询 DOI 与公开元数据")
    fields = _services().crossref_metadata(doi) if doi else {}
    if not fields and title:
        try:
            result = _services().http_json(
                "https://api.crossref.org/works?"
                + _services().urlencode({"query.bibliographic": title, "rows": 5}),
                timeout=7,
            )
            works = (result.get("message") or {}).get("items") or []
            best = max(
                works,
                key=lambda work: _services().title_similarity(
                    title, _services().clean_text((work.get("title") or [""])[0], 1000)
                ),
                default={},
            )
            candidate_title = _services().clean_text(
                (best.get("title") or [""])[0], 1000
            )
            if (
                candidate_title
                and _services().title_similarity(title, candidate_title) >= 0.72
            ):
                authors = best.get("author") or []
                date = (
                    best.get("published-print")
                    or best.get("published-online")
                    or best.get("created")
                    or {}
                )
                year = ((date.get("date-parts") or [[None]])[0] or [None])[0]
                fields = {
                    "title": candidate_title,
                    "authors": ", ".join(
                        _services().clean_text(
                            " ".join(
                                part
                                for part in (item.get("given"), item.get("family"))
                                if part
                            ),
                            200,
                        )
                        for item in authors[:20]
                    ),
                    "year": year,
                    "doi": _services().norm_doi(best.get("DOI", "")),
                    "abstract": _services().clean_text(
                        _services().re.sub(
                            r"<[^>]+>", " ", str(best.get("abstract") or "")
                        ),
                        30000,
                    ),
                    "venue": _services().clean_text(
                        (best.get("container-title") or [""])[0], 1000
                    ),
                    "url": _services().clean_text(best.get("URL", ""), 3000),
                }
        except Exception:
            fields = {}
    source = "Crossref" if fields else ""
    settings = _services().model_settings()
    ai_error = ""
    if citation and settings.get("enabled") and _services().model_api_key():
        prompt = "你是学术文献元数据抽取器。严格只从用户提供的引用文本提取，不得凭常识补全或猜测。返回 JSON 对象，字段 title,authors,year,doi,arxiv_id,venue,abstract,url；缺失用空字符串或 null。"
        if progress_callback:
            progress_callback("model_request", 1, 2, "正在从引用文本识别缺失字段")
        try:
            ai = _services().model_json_request_for_task(
                prompt,
                {"existing": {"title": title, "doi": doi}, "citation_text": citation},
                "fast",
                timeout=60,
                max_output_tokens=900,
            )
            for key in (
                "title",
                "authors",
                "year",
                "doi",
                "arxiv_id",
                "venue",
                "abstract",
                "url",
            ):
                value = ai.get(key)
                if value not in (None, "", []):
                    fields.setdefault(key, value)
            source = (source + " + " if source else "") + "AI 引用识别"
        except Exception as exc:
            ai_error = _services().clean_text(
                _services().model_error_detail(exc, _services().model_api_key()), 500
            )
    if isinstance(fields.get("authors"), list):
        fields["authors"] = ", ".join(
            _services().clean_text(value, 200) for value in fields["authors"][:20]
        )
    if fields.get("year"):
        try:
            fields["year"] = int(fields["year"])
            if not 1800 <= fields["year"] <= 2200:
                fields.pop("year", None)
        except (TypeError, ValueError):
            fields.pop("year", None)
    for key, limit in (
        ("title", 1000),
        ("authors", 4000),
        ("abstract", 30000),
        ("venue", 1000),
        ("url", 3000),
        ("arxiv_id", 100),
    ):
        if key in fields:
            fields[key] = _services().clean_text(fields[key], limit)
    if fields.get("doi"):
        fields["doi"] = _services().norm_doi(fields["doi"])
    if fields.get("arxiv_id"):
        fields["arxiv_id"] = _services().norm_arxiv(fields["arxiv_id"])
    warning = "结果来自公开元数据或你提供的引用文本；保存前请核对标题、作者和年份。"
    if ai_error:
        warning += (
            f" AI 引用识别未完成：{ai_error}。已保留可用的公开元数据，可稍后重试。"
        )
    return {
        "fields": fields,
        "source": source or "未找到可验证的公开元数据",
        "warning": warning,
        "ai_error": ai_error,
        "partial": bool(ai_error),
    }


def _metadata_draft_input(data: dict) -> dict:
    return {
        "title": _services().clean_text(data.get("title", ""), 1000),
        "doi": _services().norm_doi(_services().clean_text(data.get("doi", ""), 300)),
        "citation": _services().clean_text(data.get("citation", ""), 20000),
    }


def metadata_draft_job_snapshot(row) -> dict:
    if not row:
        return {}
    item = dict(row)
    try:
        item["result"] = _services().json.loads(item.pop("result_json", "{}") or "{}")
    except (TypeError, ValueError, _services().json.JSONDecodeError):
        item["result"] = {}
    item.pop("input_json", None)
    item.pop("request_hash", None)
    try:
        started = _services().datetime.fromisoformat(
            item.get("started_at") or item.get("created_at")
        )
        ended = _services().datetime.fromisoformat(
            item.get("completed_at") or _services().now_iso()
        )
        item["duration_ms"] = max(0, round((ended - started).total_seconds() * 1000, 2))
    except (TypeError, ValueError):
        item["duration_ms"] = 0
    return item


def get_metadata_draft_job(job_id: str) -> dict | None:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return _services().metadata_draft_job_snapshot(row) if row else None


def _update_metadata_draft_job(job_id: str, **updates) -> dict:
    allowed = {
        "status",
        "stage",
        "progress_current",
        "progress_total",
        "message",
        "result_json",
        "error",
        "input_json",
        "attempts",
        "started_at",
        "updated_at",
        "completed_at",
    }
    values = {key: value for key, value in updates.items() if key in allowed}
    values.setdefault("updated_at", _services().now_iso())
    assignments = ",".join(f"{key}=?" for key in values)
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            f"UPDATE metadata_draft_jobs SET {assignments} WHERE id=?",
            (*values.values(), job_id),
        )
        row = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return _services().metadata_draft_job_snapshot(row) if row else {}


def run_metadata_draft_job(job_id: str) -> None:
    job = _services().get_metadata_draft_job(job_id)
    if not job or job.get("status") != "queued":
        return
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT input_json FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
    if not row:
        return
    try:
        payload = _services().json.loads(row["input_json"] or "{}")
        _services()._update_metadata_draft_job(
            job_id,
            status="running",
            stage="crossref",
            progress_current=0,
            progress_total=2,
            message="正在查询 DOI 与公开元数据",
            started_at=_services().now_iso(),
            error="",
        )

        def progress(stage, current, total, message):
            _services()._update_metadata_draft_job(
                job_id,
                stage=_services().clean_text(stage, 80),
                progress_current=max(0, int(current or 0)),
                progress_total=max(1, int(total or 1)),
                message=_services().clean_text(message, 300),
            )

        result = _services().complete_paper_metadata_draft(
            payload, progress_callback=progress
        )
        status = "partial" if result.get("partial") else "success"
        result_json = _services().json.dumps(result, ensure_ascii=False)
        _services()._update_metadata_draft_job(
            job_id,
            status=status,
            stage="completed",
            progress_current=2,
            progress_total=2,
            message=(
                "已完成，部分字段可重试" if status == "partial" else "元数据草稿已生成"
            ),
            result_json=result_json,
            input_json=(
                "{}"
                if status == "success"
                else _services().json.dumps(payload, ensure_ascii=False)
            ),
            error=_services().clean_text(result.get("ai_error", ""), 500),
            completed_at=_services().now_iso(),
        )
    except Exception as exc:
        detail = _services().clean_text(
            _services().model_error_detail(exc, _services().model_api_key()), 700
        )
        _services()._update_metadata_draft_job(
            job_id,
            status="failed",
            stage="failed",
            message="元数据识别失败",
            error=detail,
            completed_at=_services().now_iso(),
        )


def _mark_metadata_draft_interrupted_for_shutdown(
    job_id: str, message: str | None = None
) -> None:
    """Persist a retryable state when metadata work cannot be queued.

    A request can arrive after the service has started shutting down but
    before the executor has fully closed.  Treat that narrow race as a normal
    durable interruption instead of leaving a misleading ``queued`` row.
    """
    if message is None:
        message = _services().RUNTIME_SHUTDOWN_MESSAGE
    try:
        _services()._update_metadata_draft_job(
            job_id,
            status="interrupted",
            stage="interrupted",
            message=message,
            error="服务重启时任务未完成",
            completed_at=_services().now_iso(),
        )
    except Exception:
        pass


def _enqueue_metadata_draft_job(job_id: str) -> None:
    """Submit metadata work with the same shutdown guarantees as AI jobs."""
    if _services().STOP_EVENT.is_set():
        _services()._mark_metadata_draft_interrupted_for_shutdown(job_id)
        raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
    with _services().METADATA_DRAFT_EXECUTOR_LOCK:
        if _services().STOP_EVENT.is_set():
            _services()._mark_metadata_draft_interrupted_for_shutdown(job_id)
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
        if _services().METADATA_DRAFT_EXECUTOR is None:
            _services().METADATA_DRAFT_EXECUTOR = _services().ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="metadata-draft"
            )
        executor = _services().METADATA_DRAFT_EXECUTOR
        try:
            executor.submit(_services().run_metadata_draft_job, job_id)
        except RuntimeError as exc:
            if _services().METADATA_DRAFT_EXECUTOR is executor:
                _services().METADATA_DRAFT_EXECUTOR = None
            _services()._mark_metadata_draft_interrupted_for_shutdown(job_id)
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE) from exc


def submit_metadata_draft_job(data: dict) -> dict:
    payload = _services()._metadata_draft_input(data)
    if not any(payload.values()):
        raise ValueError("请至少填写论文标题、DOI 或引用文本")
    settings = _services().resolve_model_route(_services().model_settings(), "fast")
    identity = {
        "input": payload,
        "model": settings.get("model", ""),
        "protocol": settings.get("protocol", ""),
        "endpoint": _services().model_endpoint(settings),
        "prompt_version": "metadata-draft-v2",
    }
    request_hash = (
        _services()
        .hashlib.sha256(
            _services()
            .json.dumps(identity, ensure_ascii=False, sort_keys=True)
            .encode("utf-8")
        )
        .hexdigest()
    )
    with _services().DB_LOCK, _services().db_session() as db:
        previous = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE request_hash=? ORDER BY created_at DESC LIMIT 1",
            (request_hash,),
        ).fetchone()
        if previous and previous["status"] in {"queued", "running"}:
            return {
                "job": _services().metadata_draft_job_snapshot(previous),
                "deduplicated": True,
            }
        if previous and previous["status"] in {
            "success",
            "partial",
            "failed",
            "interrupted",
        }:
            return {
                "job": _services().metadata_draft_job_snapshot(previous),
                "deduplicated": True,
                "reused": previous["status"] in {"success", "partial"},
            }
        job_id = str(_services().uuid.uuid4())
        created = _services().now_iso()
        db.execute(
            "INSERT INTO metadata_draft_jobs(id,request_hash,status,stage,message,input_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                job_id,
                request_hash,
                "queued",
                "queued",
                "已加入元数据识别队列",
                _services().json.dumps(payload, ensure_ascii=False),
                created,
                created,
            ),
        )
        row = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
    _services()._enqueue_metadata_draft_job(job_id)
    return {"job": _services().metadata_draft_job_snapshot(row), "deduplicated": False}


def retry_metadata_draft_job(job_id: str) -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row:
            raise LookupError("元数据识别任务不存在")
        if row["status"] in {"queued", "running"}:
            return {
                "job": _services().metadata_draft_job_snapshot(row),
                "deduplicated": True,
            }
        if row["status"] not in {"partial", "failed", "interrupted"}:
            return {
                "job": _services().metadata_draft_job_snapshot(row),
                "deduplicated": True,
            }
        payload = _services().json.loads(row["input_json"] or "{}")
        if not any(payload.values()):
            raise ValueError("任务没有可重试的原始输入，请重新提交标题、DOI 或引用文本")
        db.execute(
            "UPDATE metadata_draft_jobs SET status='queued',stage='queued',progress_current=0,message='已加入重试队列',error='',attempts=attempts+1,started_at='',updated_at=?,completed_at='' WHERE id=?",
            (_services().now_iso(), job_id),
        )
        updated = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
    _services()._enqueue_metadata_draft_job(job_id)
    return {
        "job": _services().metadata_draft_job_snapshot(updated),
        "deduplicated": False,
    }


def resume_metadata_draft_jobs() -> None:
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT id FROM metadata_draft_jobs WHERE status IN ('queued','running') OR (status='interrupted' AND error='服务重启时任务未完成') ORDER BY created_at"
        ).fetchall()
        db.execute(
            "UPDATE metadata_draft_jobs SET status='queued',stage='queued',message='服务启动后继续处理任务',error='',started_at='',updated_at=? WHERE status IN ('queued','running') OR (status='interrupted' AND error='服务重启时任务未完成')",
            (_services().now_iso(),),
        )
    for row in rows:
        try:
            _services()._enqueue_metadata_draft_job(row["id"])
        except RuntimeError:
            # A shutdown can race startup recovery.  The row is already
            # marked interrupted by the enqueue helper and can be retried on
            # the next process start.
            continue


def _ingest_pdf_item(item_id: str, job_id: str) -> None:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT * FROM pdf_ingest_items WHERE id=? AND job_id=?", (item_id, job_id)
        ).fetchone()
    if not row:
        return

    def update(**updates):
        updates["updated_at"] = _services().now_iso()
        assignments = ",".join(f"{key}=?" for key in updates)
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                f"UPDATE pdf_ingest_items SET {assignments} WHERE id=?",
                (*updates.values(), item_id),
            )
            counts = db.execute(
                "SELECT SUM(status='success'),SUM(status='failed'),SUM(duplicate=1),SUM(ai_status='completed'),SUM(ai_status='failed') FROM pdf_ingest_items WHERE job_id=?",
                (job_id,),
            ).fetchone()
            job_updates = {
                "completed": int(counts[0] or 0),
                "failed": int(counts[1] or 0),
                "duplicates": int(counts[2] or 0),
                "ai_completed": int(counts[3] or 0),
                "ai_failed": int(counts[4] or 0),
                "updated_at": _services().now_iso(),
            }
            if updates.get("stage"):
                job_updates["stage"] = updates["stage"]
            db.execute(
                "UPDATE pdf_ingest_jobs SET completed=?,failed=?,duplicates=?,ai_completed=?,ai_failed=?,stage=COALESCE(?,stage),updated_at=? WHERE id=?",
                (
                    job_updates["completed"],
                    job_updates["failed"],
                    job_updates["duplicates"],
                    job_updates["ai_completed"],
                    job_updates["ai_failed"],
                    job_updates.get("stage"),
                    job_updates["updated_at"],
                    job_id,
                ),
            )

    try:
        update(status="running", stage="reading")
        source_file = _services().Path(row["staged_path"])
        if row["source_path"]:
            current = _services().Path(row["source_path"])
            stat = current.stat()
            if row["source_mtime"] is not None and (
                stat.st_mtime != row["source_mtime"]
                or stat.st_size != row["source_size"]
            ):
                raise ValueError("监控文件在排队后发生变化，请重新扫描")
            source_file = current
        if not source_file.is_file() and row["attachment_id"]:
            with _services().DB_LOCK, _services().db_session() as db:
                stored = db.execute(
                    "SELECT stored_name FROM attachments WHERE id=?",
                    (row["attachment_id"],),
                ).fetchone()
            if stored:
                source_file = (
                    _services().active_files_dir()
                    / _services().Path(stored["stored_name"]).name
                )
        remove_staged_source = (
            not row["source_path"]
            and source_file.is_file()
            and _services().DATA_DIR.joinpath("ingest-staging").resolve()
            in source_file.resolve().parents
        )
        metadata, preview = _services().pdf_local_metadata(source_file, row["filename"])
        with _services().DB_LOCK, _services().db_session() as db:
            job = db.execute(
                "SELECT options_json FROM pdf_ingest_jobs WHERE id=?", (job_id,)
            ).fetchone()
        options = _services()._json_object(job[0] if job else "{}")
        metadata["topic"] = _services().clean_text(options.get("topic", ""), 300)
        digest = _services().sha256_file(source_file)
        with _services().DB_LOCK, _services().db_session() as db:
            existing_attachment = db.execute(
                "SELECT paper_id FROM attachments WHERE sha256=? ORDER BY added_at LIMIT 1",
                (digest,),
            ).fetchone()
        crossref = _services().crossref_metadata(metadata.get("doi", ""))
        merged = dict(metadata)
        for key, value in crossref.items():
            if value and (not merged.get(key) or key == "title"):
                merged[key] = value
        paper_id = existing_attachment[0] if existing_attachment else ""
        created = False
        if not paper_id:
            paper_id, created = _services().upsert_paper(
                merged,
                source="pdf_import",
                external_id=merged.get("doi") or merged.get("arxiv_id", ""),
                raw={
                    "filename": row["filename"],
                    "metadata_source": "crossref" if crossref else "pdf_local",
                },
            )
        else:
            _services()._fill_paper_metadata(paper_id, merged, "pdf_import")
        update(
            stage="storing",
            paper_id=paper_id,
            metadata_source="Crossref" if crossref else "PDF 文本",
        )
        attachment, duplicate = _services().store_pdf_attachment(
            paper_id, row["filename"], source_file
        )
        if duplicate:
            summary = _services().extraction_summary(paper_id)
            page_count = next(
                (
                    int(item.get("page_count") or 0)
                    for item in summary["pages"]
                    if item.get("attachment_id") == attachment["id"]
                ),
                0,
            )
            if not page_count:
                page_count = _services().extract_attachment_pages(attachment["id"])[
                    "page_count"
                ]
        else:
            page_count = _services().extract_attachment_pages(attachment["id"])[
                "page_count"
            ]
        update(
            stage="metadata",
            attachment_id=attachment["id"],
            sha256=attachment["sha256"],
            page_count=page_count,
            duplicate=int(duplicate),
        )
        ai_status = "skipped"
        if not preview:
            update(
                error="没有提取到可用文字；这可能是扫描版 PDF，需要 OCR 后才能补全元数据"
            )
        if (
            options.get("auto_ai")
            and _services().model_settings().get("enabled")
            and _services().model_api_key()
            and preview
        ):
            try:
                update(stage="ai_metadata", ai_status="running")
                prompt = "你是论文元数据整理器。只依据给定 PDF 前几页文本，返回一个 JSON 对象。不要编造；不确定字段返回空字符串或 null。字段：title,authors,year,doi,arxiv_id,venue,abstract,keywords。authors 用字符串，keywords 用数组。"
                ai = _services().model_json_request_for_task(
                    prompt,
                    {"filename": row["filename"], "text": preview[:50000]},
                    "fast",
                    timeout=90,
                    max_output_tokens=900,
                )
                normalized = {
                    key: ai.get(key)
                    for key in (
                        "title",
                        "authors",
                        "year",
                        "doi",
                        "arxiv_id",
                        "venue",
                        "abstract",
                    )
                    if ai.get(key) not in (None, "")
                }
                if isinstance(normalized.get("authors"), list):
                    normalized["authors"] = ", ".join(
                        _services().clean_text(author, 200)
                        for author in normalized["authors"][:20]
                    )
                if normalized.get("year"):
                    try:
                        normalized["year"] = int(normalized["year"])
                        if not 1800 <= normalized["year"] <= 2200:
                            normalized.pop("year", None)
                    except (TypeError, ValueError):
                        normalized.pop("year", None)
                for key, limit in (
                    ("title", 1000),
                    ("authors", 4000),
                    ("venue", 1000),
                    ("abstract", 30000),
                ):
                    if key in normalized:
                        normalized[key] = _services().clean_text(normalized[key], limit)
                if normalized.get("doi"):
                    normalized["doi"] = _services().norm_doi(normalized["doi"])
                    if not _services().re.fullmatch(
                        r"10\.\d{4,9}/\S+", normalized["doi"]
                    ):
                        normalized.pop("doi", None)
                if normalized.get("arxiv_id"):
                    normalized["arxiv_id"] = _services().norm_arxiv(
                        normalized["arxiv_id"]
                    )
                    if not _services().re.fullmatch(
                        r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})",
                        normalized["arxiv_id"],
                        _services().re.I,
                    ):
                        normalized.pop("arxiv_id", None)
                if normalized:
                    _services()._fill_paper_metadata(
                        paper_id, normalized, "pdf_ai", replace_title=created
                    )
                ai_status = "completed"
                update(
                    ai_status=ai_status,
                    metadata_source=("Crossref + AI" if crossref else "PDF 文本 + AI"),
                )
            except Exception as exc:
                ai_status = "failed"
                update(ai_status=ai_status, error=f"AI 补全失败：{str(exc)[:300]}")
        else:
            update(ai_status=ai_status)
        try:
            if remove_staged_source:
                source_file.unlink(missing_ok=True)
        except OSError:
            pass
        update(status="success", stage="completed")
    except Exception as exc:
        update(status="failed", stage="failed", error=str(exc)[:700])


def _run_pdf_ingest_job(job_id: str) -> None:
    with _services().DB_LOCK, _services().db_session() as db:
        items = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM pdf_ingest_items WHERE job_id=? AND status IN ('queued','running') ORDER BY created_at",
                (job_id,),
            ).fetchall()
        ]
        db.execute(
            "UPDATE pdf_ingest_jobs SET status='running',stage='processing',updated_at=? WHERE id=?",
            (_services().now_iso(), job_id),
        )
    with _services().ThreadPoolExecutor(
        max_workers=min(_services().PDF_IMPORT_CONCURRENCY, max(1, len(items))),
        thread_name_prefix="pdf-ingest",
    ) as pool:
        futures = [
            pool.submit(_services()._ingest_pdf_item, item_id, job_id)
            for item_id in items
        ]
        for future in futures:
            future.result()
    with _services().DB_LOCK, _services().db_session() as db:
        counts = db.execute(
            "SELECT COUNT(*),SUM(status='success'),SUM(status='failed'),SUM(ai_status='completed'),SUM(ai_status='failed'),SUM(duplicate=1) FROM pdf_ingest_items WHERE job_id=?",
            (job_id,),
        ).fetchone()
        total, completed, failed, ai_completed, ai_failed, duplicates = [
            int(value or 0) for value in counts
        ]
        status = "success" if not failed else ("partial" if completed else "failed")
        db.execute(
            "UPDATE pdf_ingest_jobs SET status=?,stage='completed',total=?,completed=?,failed=?,duplicates=?,ai_completed=?,ai_failed=?,updated_at=?,completed_at=? WHERE id=?",
            (
                status,
                total,
                completed,
                failed,
                duplicates,
                ai_completed,
                ai_failed,
                _services().now_iso(),
                _services().now_iso(),
                job_id,
            ),
        )


def _mark_pdf_ingest_interrupted_for_shutdown(
    job_id: str, message: str | None = None
) -> None:
    """Keep a PDF batch visible and resumable when submission races shutdown."""
    if message is None:
        message = _services().RUNTIME_SHUTDOWN_MESSAGE
    try:
        with _services().DB_LOCK, _services().db_session() as db:
            now = _services().now_iso()
            db.execute(
                "UPDATE pdf_ingest_jobs SET status='interrupted',stage='interrupted',error=?,updated_at=?,completed_at=? WHERE id=? AND status IN ('queued','running')",
                ("服务重启时任务未完成", now, now, job_id),
            )
    except Exception:
        pass


def _enqueue_pdf_ingest_job(job_id: str) -> None:
    """Submit a durable PDF batch without creating a shutdown zombie."""
    if _services().STOP_EVENT.is_set():
        _services()._mark_pdf_ingest_interrupted_for_shutdown(job_id)
        raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
    with _services().PDF_INGEST_EXECUTOR_LOCK:
        if _services().STOP_EVENT.is_set():
            _services()._mark_pdf_ingest_interrupted_for_shutdown(job_id)
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
        if _services().PDF_INGEST_EXECUTOR is None:
            _services().PDF_INGEST_EXECUTOR = _services().ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="pdf-ingest-job"
            )
        executor = _services().PDF_INGEST_EXECUTOR
        try:
            executor.submit(_services()._run_pdf_ingest_job, job_id)
        except RuntimeError as exc:
            if _services().PDF_INGEST_EXECUTOR is executor:
                _services().PDF_INGEST_EXECUTOR = None
            _services()._mark_pdf_ingest_interrupted_for_shutdown(job_id)
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE) from exc


def submit_pdf_ingest_job(job_id: str) -> None:
    _services()._enqueue_pdf_ingest_job(job_id)


def resume_pdf_ingest_jobs() -> None:
    with _services().DB_LOCK, _services().db_session() as db:
        jobs = [
            row[0]
            for row in db.execute(
                "SELECT DISTINCT i.job_id FROM pdf_ingest_items i JOIN pdf_ingest_jobs j ON j.id=i.job_id "
                "WHERE i.status IN ('queued','running') OR (j.status='interrupted' AND j.error='服务重启时任务未完成')"
            ).fetchall()
        ]
        db.execute(
            "UPDATE pdf_ingest_items SET status='queued',stage='queued',updated_at=? WHERE status='running'",
            (_services().now_iso(),),
        )
        db.execute(
            "UPDATE pdf_ingest_jobs SET status='queued',stage='queued',error='',updated_at=?,completed_at='' WHERE status='running' OR (status='interrupted' AND error='服务重启时任务未完成')",
            (_services().now_iso(),),
        )
    for job_id in jobs:
        try:
            _services()._enqueue_pdf_ingest_job(job_id)
        except RuntimeError:
            continue


def create_pdf_ingest_job(files: list[dict], options: dict | None = None) -> dict:
    options = dict(options or {})
    files = files[:200]
    if not files:
        raise ValueError("请至少选择一个 PDF 文件")
    job_id = str(_services().uuid.uuid4())
    staging = _services().DATA_DIR / "ingest-staging"
    staging.mkdir(parents=True, exist_ok=True)
    now = _services().now_iso()
    encoded_total = 0
    normalized_files = [
        file for file in files if str(file.get("filename", "")).lower().endswith(".pdf")
    ]
    if not normalized_files:
        raise ValueError("所选文件中没有 PDF")
    prepared = []
    for file in normalized_files:
        filename = (
            _services()
            .Path(_services().clean_text(file.get("filename", "paper.pdf"), 250))
            .name
        )
        item_id = str(_services().uuid.uuid4())
        is_folder_file = bool(file.get("source_path"))
        staged_upload = bool(file.get("staged_upload_path"))
        staged = (
            _services().Path(str(file["source_path"])).resolve()
            if is_folder_file
            else (
                _services().Path(str(file["staged_upload_path"])).resolve()
                if staged_upload
                else staging / f"{item_id}.pdf"
            )
        )
        error = ""
        if is_folder_file:
            if not staged.is_file():
                error = "监控文件不存在或不可访问"
            elif int(file.get("source_size") or 0) > 100 * 1024 * 1024:
                error = "单个 PDF 限制为 100 MB"
        elif staged_upload:
            if not staged.is_file():
                error = "上传暂存文件不存在"
            elif staged.stat().st_size > 100 * 1024 * 1024:
                error = "单个 PDF 限制为 100 MB"
            else:
                with staged.open("rb") as handle:
                    if not handle.read(4).startswith(b"%PDF"):
                        error = "文件内容不像有效 PDF"
        else:
            try:
                content = (
                    file.get("data_bytes")
                    if isinstance(file.get("data_bytes"), bytes)
                    else None
                )
                if content is None:
                    encoded = str(file.get("data", ""))
                    encoded_total += len(encoded)
                    if encoded_total > 245 * 1024 * 1024:
                        raise ValueError(
                            "批量 PDF 上传总量超过 180 MB 限制，请分批选择"
                        )
                    if len(encoded) > 134 * 1024 * 1024:
                        raise ValueError("单个 PDF 限制为 100 MB")
                    content = _services().base64.b64decode(encoded, validate=True)
                if len(content) > 100 * 1024 * 1024:
                    raise ValueError("单个 PDF 限制为 100 MB")
                if not content.startswith(b"%PDF"):
                    raise ValueError("文件内容不像有效 PDF")
                staged.write_bytes(content)
            except Exception as exc:
                error = str(exc)[:300] or "PDF 文件无法读取"
        prepared.append(
            {
                "id": item_id,
                "filename": filename,
                "source_path": str(staged) if is_folder_file else "",
                "source_mtime": file.get("source_mtime"),
                "source_size": file.get("source_size"),
                "staged_path": str(staged),
                "error": error,
            }
        )
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "INSERT INTO pdf_ingest_jobs(id,status,stage,total,options_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (
                job_id,
                "queued",
                "queued",
                len(prepared),
                _services().json.dumps(
                    {
                        "auto_ai": bool(options.get("auto_ai", True)),
                        "topic": _services().clean_text(options.get("topic", ""), 300),
                    },
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )
        for item in prepared:
            status = "failed" if item["error"] else "queued"
            db.execute(
                "INSERT INTO pdf_ingest_items(id,job_id,filename,source_path,source_mtime,source_size,staged_path,status,stage,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item["id"],
                    job_id,
                    item["filename"],
                    item["source_path"],
                    item["source_mtime"],
                    item["source_size"],
                    item["staged_path"],
                    status,
                    "failed" if item["error"] else "queued",
                    item["error"],
                    now,
                    now,
                ),
            )
    _services().submit_pdf_ingest_job(job_id)
    return _services().pdf_ingest_job_snapshot(job_id)


def pdf_ingest_job_snapshot(job_id: str) -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        job = db.execute(
            "SELECT * FROM pdf_ingest_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not job:
            return {}
        result = dict(job)
        result["options"] = _services()._json_object(result.pop("options_json", "{}"))
        result["items"] = [
            dict(row)
            for row in db.execute(
                "SELECT id,filename,status,stage,paper_id,attachment_id,sha256,page_count,error,metadata_source,ai_status,duplicate,created_at,updated_at FROM pdf_ingest_items WHERE job_id=? ORDER BY created_at",
                (job_id,),
            ).fetchall()
        ]
        return result


def list_pdf_ingest_jobs(limit: int = 20) -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        jobs = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM pdf_ingest_jobs ORDER BY created_at DESC LIMIT ?",
                (min(max(limit, 1), 100),),
            ).fetchall()
        ]
        for job in jobs:
            job["items"] = [
                dict(row)
                for row in db.execute(
                    "SELECT id,filename,status,stage,paper_id,attachment_id,sha256,page_count,error,metadata_source,ai_status,duplicate,created_at,updated_at FROM pdf_ingest_items WHERE job_id=? ORDER BY created_at",
                    (job["id"],),
                ).fetchall()
            ]
        folders = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM paper_folder_sources ORDER BY path"
            ).fetchall()
        ]
    return {"jobs": jobs, "folders": folders}


def register_paper_folder(data: dict) -> dict:
    raw_path = _services().clean_text(data.get("path", ""), 2000)
    if not raw_path:
        raise ValueError("请输入本机论文文件夹路径")
    path = str(_services().Path(raw_path).expanduser().resolve())
    if not _services().Path(path).is_dir():
        raise ValueError("文件夹不存在或不可访问")
    folder_id = str(_services().uuid.uuid4())
    now = _services().now_iso()
    interval = min(max(int(data.get("interval_minutes", 30)), 5), 10080)
    enabled = bool(data.get("enabled", True))
    with _services().DB_LOCK, _services().db_session() as db:
        existing = db.execute(
            "SELECT id FROM paper_folder_sources WHERE path=?", (path,)
        ).fetchone()
        if existing:
            folder_id = existing[0]
            db.execute(
                "UPDATE paper_folder_sources SET recursive=?,auto_ai=?,interval_minutes=?,enabled=?,updated_at=? WHERE id=?",
                (
                    int(data.get("recursive", True)),
                    int(data.get("auto_ai", True)),
                    interval,
                    int(enabled),
                    now,
                    folder_id,
                ),
            )
        else:
            db.execute(
                "INSERT INTO paper_folder_sources(id,path,recursive,auto_ai,interval_minutes,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    folder_id,
                    path,
                    int(data.get("recursive", True)),
                    int(data.get("auto_ai", True)),
                    interval,
                    int(enabled),
                    now,
                    now,
                ),
            )
        _services().record_audit(
            db, "pdf.folder_registered", "folder", folder_id, {"path": path}
        )
        row = db.execute(
            "SELECT * FROM paper_folder_sources WHERE id=?", (folder_id,)
        ).fetchone()
    return dict(row)


def retry_pdf_ingest_job(job_id: str) -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT id,staged_path,status,ai_status,attachment_id,source_path FROM pdf_ingest_items WHERE job_id=?",
            (job_id,),
        ).fetchall()
        if not rows:
            raise ValueError("导入任务不存在")
        for row in rows:
            should_retry = row["status"] == "failed" or (
                row["status"] == "success" and row["ai_status"] == "failed"
            )
            retry_source = _services().Path(row["source_path"] or row["staged_path"])
            if not retry_source.is_file() and row["attachment_id"]:
                attachment = db.execute(
                    "SELECT stored_name FROM attachments WHERE id=?",
                    (row["attachment_id"],),
                ).fetchone()
                retry_source = (
                    _services().active_files_dir()
                    / _services().Path(attachment["stored_name"]).name
                    if attachment
                    else retry_source
                )
            if should_retry and retry_source.is_file():
                db.execute(
                    "UPDATE pdf_ingest_items SET status='queued',stage='queued',error='',updated_at=? WHERE id=?",
                    (_services().now_iso(), row["id"]),
                )
        db.execute(
            "UPDATE pdf_ingest_jobs SET status='queued',stage='queued',error='',updated_at=?,completed_at='' WHERE id=?",
            (_services().now_iso(), job_id),
        )
    _services().submit_pdf_ingest_job(job_id)
    return _services().pdf_ingest_job_snapshot(job_id)


def scan_paper_folder(folder_id: str) -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        folder = db.execute(
            "SELECT * FROM paper_folder_sources WHERE id=?", (folder_id,)
        ).fetchone()
    if not folder:
        raise ValueError("论文文件夹不存在")
    root = _services().Path(folder["path"]).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("论文文件夹不存在或不可访问")
    iterator = root.rglob("*.pdf") if folder["recursive"] else root.glob("*.pdf")
    files = []
    with _services().DB_LOCK, _services().db_session() as db:
        known = {
            (row[0], row[1], row[2])
            for row in db.execute(
                "SELECT source_path,source_mtime,source_size FROM pdf_ingest_items WHERE source_path<>''"
            ).fetchall()
        }
    for path in iterator:
        try:
            stat = path.stat()
            key = (str(path), stat.st_mtime, stat.st_size)
            if key in known:
                continue
            if (
                stat.st_size > 100 * 1024 * 1024
                or path.is_symlink()
                or root not in path.resolve().parents
            ):
                continue
            files.append(
                {
                    "filename": path.name,
                    "source_path": str(path.resolve()),
                    "source_mtime": stat.st_mtime,
                    "source_size": stat.st_size,
                }
            )
        except OSError:
            continue
        if len(files) >= 200:
            break
    result = (
        _services().create_pdf_ingest_job(files, {"auto_ai": bool(folder["auto_ai"])})
        if files
        else {"status": "success", "total": 0, "message": "没有发现新增 PDF"}
    )
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "UPDATE paper_folder_sources SET last_scan_at=?,next_scan_at=?,last_error='',updated_at=? WHERE id=?",
            (
                _services().now_iso(),
                (
                    _services().datetime.now(_services().timezone.utc)
                    + _services().timedelta(
                        minutes=int(folder["interval_minutes"] or 30)
                    )
                )
                .replace(microsecond=0)
                .isoformat(),
                _services().now_iso(),
                folder_id,
            ),
        )
        if files:
            # Keep the source identity on each durable item for future scans.
            job_id = result.get("id", "")
    return result


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
