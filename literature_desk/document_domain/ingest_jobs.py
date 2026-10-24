"""Folder scanning and PDF ingestion job lifecycle."""

from __future__ import annotations

from pathlib import Path

from ..services import app_services as _services

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


