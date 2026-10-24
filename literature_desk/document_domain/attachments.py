"""PDF attachment extraction, storage, and metadata enrichment."""

from __future__ import annotations

from pathlib import Path

from ..services import app_services as _services


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


