"""Document retrieval, OCR, page search, and citation expansion tools."""

from __future__ import annotations

from ..services import app_services as _services

def _tool_prepare_fulltext(payload: dict) -> dict:
    papers = (
        payload.get("papers") if isinstance(payload.get("papers"), list) else []
    )
    prepared, ready_count, downloadable_count, missing_count = [], 0, 0, 0
    seen_keys = set()
    with _services().DB_LOCK, _services().db_session() as db:
        for item in papers[:8]:
            if not isinstance(item, dict):
                continue
            paper_id = _services().clean_text(
                item.get("local_paper_id") or item.get("id", ""), 200
            )
            row = (
                db.execute(
                    "SELECT id,title,pdf_url,url,arxiv_id,doi FROM papers WHERE id=?",
                    (paper_id,),
                ).fetchone()
                if paper_id and not paper_id.startswith("candidate:")
                else None
            )
            # Expanded searches can return a new candidate ID for the same work.
            # Collapse those aliases before counting download/readiness gaps.
            identity = _services().candidate_identity(
                {
                    **item,
                    **(dict(row) if row else {}),
                }
            )
            identity = identity or f"id:{paper_id}"
            if identity in seen_keys:
                continue
            seen_keys.add(identity)
            attachment_count = (
                int(
                    db.execute(
                        "SELECT COUNT(*) FROM attachments WHERE paper_id=?",
                        (paper_id,),
                    ).fetchone()[0]
                )
                if row
                else 0
            )
            page_count = (
                int(
                    db.execute(
                        "SELECT COUNT(*) FROM document_pages WHERE paper_id=?",
                        (paper_id,),
                    ).fetchone()[0]
                )
                if row
                else 0
            )
            urls = (
                _services().paper_pdf_candidates(row)
                if row
                else [_services().clean_text(item.get("pdf_url", ""), 3000)]
            )
            urls = [url for url in urls if url]
            if attachment_count:
                state = "ready"
                ready_count += 1
            elif urls:
                state = "downloadable"
                downloadable_count += 1
            else:
                state = "missing"
                missing_count += 1
            prepared.append(
                {
                    "paper_id": paper_id or item.get("id", ""),
                    "title": item.get("title") or (row["title"] if row else ""),
                    "state": state,
                    "attachment_count": attachment_count,
                    "page_count": page_count,
                    "download_urls": urls[:4],
                    "requires_user_action": state == "downloadable",
                    "reason": (
                        "已有本地 PDF"
                        if state == "ready"
                        else (
                            "存在公开地址，需用户确认下载"
                            if state == "downloadable"
                            else "没有可用的公开 PDF 地址"
                        )
                    ),
                }
            )
    return {
        "papers": prepared,
        "ready_count": ready_count,
        "downloadable_count": downloadable_count,
        "missing_count": missing_count,
        "status": (
            "ready"
            if not downloadable_count and not missing_count
            else (
                "needs_user_download"
                if downloadable_count
                else "no_public_fulltext"
            )
        ),
    }


def _tool_download_fulltext(payload: dict) -> dict:
    if payload.get("confirm") is not True:
        raise PermissionError("下载公开 PDF 会访问外部数据源，需要显式确认")
    requested = (
        payload.get("papers") if isinstance(payload.get("papers"), list) else []
    )
    selected_ids = list(
        dict.fromkeys(
            _services().clean_text(value, 200)
            for value in (payload.get("paper_ids") or [])
            if _services().clean_text(value, 200)
        )
    )
    if not selected_ids:
        selected_ids = list(
            dict.fromkeys(
                _services().clean_text(item.get("local_paper_id"), 200)
                for item in requested
                if isinstance(item, dict)
                and _services().clean_text(item.get("local_paper_id"), 200)
            )
        )
    if not selected_ids or len(selected_ids) > 8:
        raise ValueError("请选择 1 到 8 篇已经导入文献库的论文下载 PDF")
    prepared = {
        _services().clean_text(item.get("local_paper_id"), 200): item
        for item in requested
        if isinstance(item, dict)
    }
    outcomes = {}

    def download_one(paper_id):
        item = prepared.get(paper_id, {})
        url_candidates = (
            item.get("download_urls")
            if isinstance(item.get("download_urls"), list)
            else []
        )
        url = url_candidates[0] if url_candidates else ""
        try:
            return {
                "ok": True,
                "paper_id": paper_id,
                "result": _services().download_public_pdf(paper_id, url),
            }
        except Exception as exc:
            return {
                "ok": False,
                "paper_id": paper_id,
                "error": _services().clean_text(str(exc), 500),
            }

    worker_count = min(
        _services().PDF_DOWNLOAD_CONCURRENCY, max(1, len(selected_ids))
    )
    with _services().ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="agent-pdf-download"
    ) as pool:
        futures = {
            pool.submit(download_one, paper_id): paper_id
            for paper_id in selected_ids
        }
        completed_count = 0
        for future in _services().as_completed(futures):
            outcome = future.result()
            outcomes[outcome["paper_id"]] = outcome
            completed_count += 1
            if payload.get("step_id"):
                _services()._agent_update_step(
                    payload.get("run_id", ""),
                    payload["step_id"],
                    progress_current=completed_count,
                    progress_total=len(selected_ids),
                    message=f"已下载 {completed_count}/{len(selected_ids)} 篇 PDF",
                )

    # Keep the API deterministic even though completion order depends on
    # network latency.  The caller can render results beside its selection.
    results = [
        {"paper_id": paper_id, **outcomes[paper_id]["result"]}
        for paper_id in selected_ids
        if outcomes.get(paper_id, {}).get("ok")
    ]
    failures = [
        {"paper_id": paper_id, "error": outcomes[paper_id]["error"]}
        for paper_id in selected_ids
        if paper_id in outcomes and not outcomes[paper_id]["ok"]
    ]
    return {
        "status": (
            "completed"
            if results and not failures
            else ("partial" if results else "failed")
        ),
        "downloaded": results,
        "failed": failures,
        "downloaded_count": len(results),
        "failed_count": len(failures),
    }


def _tool_extract_pdf_pages(payload: dict) -> dict:
    paper_ids = [
        _services().clean_text(item, 200)
        for item in (payload.get("paper_ids") or [])
        if _services().clean_text(item, 200)
    ]
    if not paper_ids and payload.get("paper_id"):
        paper_ids = [_services().clean_text(payload.get("paper_id"), 200)]
    paper_ids = list(dict.fromkeys(paper_ids))[:8]
    results, skipped = [], []

    def extract_one(paper_id):
        cached = _services().extraction_summary(paper_id)
        if cached.get("attachment_count") and cached.get(
            "attachment_count"
        ) == cached.get("extracted_attachment_count"):
            return {
                "paper_id": paper_id,
                "page_count": cached.get("page_count", 0),
                "characters": cached.get("characters", 0),
                "cached": True,
            }
        return _services().extract_paper_documents(paper_id)

    # PDF parsing is independent per paper.  A small bounded pool avoids
    # making a long Agent run wait serially while keeping SQLite writes
    # protected by DB_LOCK inside the extraction helpers.  Results are
    # sorted back into input order so artifacts remain deterministic.
    completed = {}
    worker_count = min(3, max(1, len(paper_ids)))
    with _services().ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="agent-pdf-extract"
    ) as pool:
        futures = {
            pool.submit(extract_one, paper_id): paper_id for paper_id in paper_ids
        }
        for future in _services().as_completed(futures):
            paper_id = futures[future]
            try:
                completed[paper_id] = future.result()
            except ValueError as exc:
                skipped.append({"paper_id": paper_id, "reason": str(exc)})
            except Exception as exc:
                skipped.append(
                    {
                        "paper_id": paper_id,
                        "reason": f"PDF 解析失败：{_services().clean_text(str(exc), 300)}",
                    }
                )
            if payload.get("step_id"):
                _services()._agent_update_step(
                    payload.get("run_id", ""),
                    payload["step_id"],
                    progress_current=len(completed) + len(skipped),
                    progress_total=len(paper_ids),
                    message=f"已处理 {len(completed) + len(skipped)}/{len(paper_ids)} 篇本地 PDF",
                )
    results = [
        completed[paper_id] for paper_id in paper_ids if paper_id in completed
    ]
    return {
        "papers_processed": len(results),
        "skipped": skipped,
        "results": results,
        "page_count": sum(int(item.get("page_count") or 0) for item in results),
        "characters": sum(int(item.get("characters") or 0) for item in results),
    }


def _tool_ocr_pdf_pages(payload: dict) -> dict:
    paper_ids = [
        _services().clean_text(item, 200)
        for item in (payload.get("paper_ids") or [])
        if _services().clean_text(item, 200)
    ]
    paper_ids = list(dict.fromkeys(paper_ids))[:8]
    max_pages = min(max(int(payload.get("max_pages", 24)), 1), 40)
    attempted = updated = skipped = 0
    pages_seen = []
    unavailable_reason = ""
    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        unavailable_reason = f"OCR 依赖不可用：{exc.name or str(exc)}"
        return {
            "status": "unavailable",
            "attempted": 0,
            "updated": 0,
            "skipped": 0,
            "pages": [],
            "reason": unavailable_reason,
            "paper_count": len(paper_ids),
        }
    # Build the page worklist first.  Rendering and OCR are CPU/external
    # work, so they run in a bounded pool; SQLite updates stay in the main
    # thread to avoid write contention and to make progress durable.
    page_rows = []
    for paper_id in paper_ids:
        with _services().DB_LOCK, _services().db_session() as db:
            page_rows.extend(
                dict(row)
                for row in db.execute(
                    "SELECT d.attachment_id,d.paper_id,d.page_number,d.text,d.char_count,d.quality,a.stored_name "
                    "FROM document_pages d JOIN attachments a ON a.id=d.attachment_id "
                    "WHERE d.paper_id=? AND d.quality IN ('low','empty') ORDER BY d.page_number LIMIT ?",
                    (paper_id, max_pages),
                )
            )

    language = _services().clean_text(payload.get("language", "eng"), 40) or "eng"

    def ocr_one(row):
        paper_id = row["paper_id"]
        page_number = int(row["page_number"])
        target = (
            _services().active_files_dir()
            / _services().Path(row["stored_name"]).name
        )
        base = {
            "paper_id": paper_id,
            "page": page_number,
            "attachment_id": row["attachment_id"],
        }
        if not target.is_file():
            return {**base, "status": "missing_file"}
        try:
            with fitz.open(str(target)) as document:
                page_index = page_number - 1
                if page_index < 0 or page_index >= len(document):
                    raise ValueError("页码超出 PDF 范围")
                pixmap = document[page_index].get_pixmap(
                    matrix=fitz.Matrix(2, 2), alpha=False
                )
                image = Image.frombytes(
                    "RGB", [pixmap.width, pixmap.height], pixmap.samples
                )
                text = _services().clean_text(
                    pytesseract.image_to_string(image, lang=language), 500000
                )
            chars = len(text.strip())
            if chars <= int(row.get("char_count") or 0):
                return {**base, "status": "no_improvement", "characters": chars}
            digest = _services().hashlib.sha256(text.encode("utf-8")).hexdigest()
            return {
                **base,
                "status": "updated",
                "text": text,
                "characters": chars,
                "quality": "good" if chars >= 160 else "low",
                "text_sha256": digest,
            }
        except Exception as exc:
            return {
                **base,
                "status": "failed",
                "reason": _services().clean_text(str(exc), 300),
            }

    outcomes = []
    worker_count = (
        min(_services().OCR_CONCURRENCY, max(1, len(page_rows))) if page_rows else 1
    )
    with _services().ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="agent-ocr"
    ) as pool:
        futures = [pool.submit(ocr_one, row) for row in page_rows]
        for completed_count, future in enumerate(
            _services().as_completed(futures), 1
        ):
            try:
                outcome = future.result()
            except Exception as exc:
                # ocr_one is defensive, but retain per-page isolation if a
                # dependency raises outside its normal exception path.
                outcome = {
                    "paper_id": "",
                    "page": 0,
                    "status": "failed",
                    "reason": _services().clean_text(str(exc), 300),
                }
            outcomes.append(outcome)
            if payload.get("step_id"):
                _services()._agent_update_step(
                    payload.get("run_id", ""),
                    payload["step_id"],
                    progress_current=completed_count,
                    progress_total=len(page_rows),
                    message=f"已完成 OCR {completed_count}/{len(page_rows)} 页",
                )

    # Completion order is intentionally normalized for stable API output.
    order = {
        (
            str(row["paper_id"]),
            int(row["page_number"]),
            str(row["attachment_id"]),
        ): index
        for index, row in enumerate(page_rows)
    }
    outcomes.sort(
        key=lambda item: order.get(
            (
                str(item.get("paper_id", "")),
                int(item.get("page", 0)),
                str(item.get("attachment_id", "")),
            ),
            len(order),
        )
    )
    for outcome in outcomes:
        attempted += 1
        status = outcome.get("status")
        if status == "updated":
            try:
                with _services().DB_LOCK, _services().db_session() as db:
                    db.execute(
                        "UPDATE document_pages SET text=?,char_count=?,quality=?,extractor=?,text_sha256=?,extracted_at=? WHERE attachment_id=? AND page_number=?",
                        (
                            outcome["text"],
                            outcome["characters"],
                            outcome["quality"],
                            "pytesseract",
                            outcome["text_sha256"],
                            _services().now_iso(),
                            outcome["attachment_id"],
                            outcome["page"],
                        ),
                    )
                updated += 1
            except Exception as exc:
                status = "failed"
                outcome["reason"] = _services().clean_text(str(exc), 300)
        if status != "updated":
            skipped += 1
        page_result = {
            "paper_id": outcome.get("paper_id", ""),
            "page": int(outcome.get("page", 0)),
            "status": status,
        }
        for key in ("characters", "text_sha256", "reason"):
            if key in outcome:
                page_result[key] = outcome[key]
        pages_seen.append(page_result)
    status = (
        "updated"
        if updated
        else ("not_needed" if attempted == 0 else "no_improvement")
    )
    return {
        "status": status,
        "attempted": attempted,
        "updated": updated,
        "skipped": skipped,
        "pages": pages_seen,
        "paper_count": len(paper_ids),
        "reason": unavailable_reason,
    }


def _tool_render_pdf_page(payload: dict) -> dict:
    paper_id = _services().clean_text(payload.get("paper_id", ""), 200)
    page = max(1, int(payload.get("page", 1)))
    with _services().DB_LOCK, _services().db_session() as db:
        attachment = db.execute(
            "SELECT id,stored_name,sha256 FROM attachments WHERE paper_id=? ORDER BY added_at LIMIT 1",
            (paper_id,),
        ).fetchone()
    if not attachment:
        raise ValueError("论文没有本地 PDF 附件")
    rendered = _services().render_pdf_page_visual(dict(attachment), page)
    return {
        "paper_id": paper_id,
        "page": page,
        "attachment_id": attachment["id"],
        "url": "/visual-pages/" + _services().Path(rendered["path"]).name,
        "sha256": rendered.get("sha256", ""),
    }


def _tool_search_document(payload: dict) -> dict:
    return {
        "paper_id": payload.get("paper_id", ""),
        "query": payload.get("query", ""),
        "pages": _services().search_document_pages(
            _services().clean_text(payload.get("paper_id", ""), 200),
            _services().clean_text(payload.get("query", ""), 300),
            min(max(int(payload.get("limit", 20)), 1), 50),
        ),
    }


def _tool_expand_citations(payload: dict) -> dict:
    # Agent expansion is a read-only candidate lane.  The interactive
    # paper-details endpoint still calls expand_openalex_relations() with
    # its default persist=True, while this path never imports every hit.
    requested = (
        payload.get("papers") if isinstance(payload.get("papers"), list) else []
    )
    seed_items, seen_seeds = [], set()

    def add_seed(item: dict, local_id: str, identifier: str) -> None:
        """Add one citation seed using a work identity, not a transient ID."""
        if not identifier:
            return
        identity = (
            _services().candidate_identity({**item, "local_paper_id": local_id})
            or identifier
        )
        if identity in seen_seeds:
            return
        seed_items.append((identifier, dict(item), local_id))
        seen_seeds.add(identity)

    for item in requested:
        if not isinstance(item, dict):
            continue
        local_id = _services().clean_text(
            item.get("local_paper_id") or item.get("paper_id"), 200
        )
        # Candidate IDs are intentionally ephemeral snapshots, not rows in
        # `papers`; use their DOI/OpenAlex identity as the remote seed.
        if local_id.startswith("candidate:"):
            local_id = ""
        identifier = local_id or _services().clean_text(
            item.get("openalex_id")
            or item.get("external_id")
            or item.get("doi")
            or "",
            300,
        )
        add_seed(item, local_id, identifier)
    single_id = _services().clean_text(payload.get("paper_id", ""), 200)
    if single_id and not single_id.startswith("candidate:"):
        add_seed({"local_paper_id": single_id}, single_id, single_id)
    goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else {}
    goal_id = _services().clean_text(goal.get("id", ""), 80)
    if goal_id:
        # A follow-up run must retain the user's previously imported seeds
        # even when the current search is candidate-only.  This keeps the
        # citation graph connected across years without importing anything.
        with _services().DB_LOCK, _services().db_session() as db:
            history_rows = db.execute(
                "SELECT local_paper_id,title,doi,arxiv_id,metadata_json FROM research_goal_papers WHERE goal_id=? ORDER BY last_seen_at DESC LIMIT 20",
                (goal_id,),
            ).fetchall()
        for row in history_rows:
            local_id = _services().clean_text(row["local_paper_id"], 200)
            if local_id.startswith("candidate:"):
                local_id = ""
            try:
                metadata = _services().json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError, _services().json.JSONDecodeError):
                metadata = {}
            item = {
                "local_paper_id": local_id,
                "title": row["title"],
                "doi": row["doi"],
                "arxiv_id": row["arxiv_id"],
                "openalex_id": metadata.get("openalex_id", ""),
                "external_id": metadata.get("external_id", ""),
            }
            identifier = local_id or _services().clean_text(
                item.get("openalex_id")
                or item.get("external_id")
                or item.get("doi")
                or "",
                300,
            )
            add_seed(item, local_id, identifier)
    seed_items = seed_items[:4]
    if not seed_items:
        return {
            "status": "skipped",
            "expanded": False,
            "reason": "没有已导入且具有 DOI/OpenAlex 标识的种子论文",
            "papers": [],
            "candidate_ids": [],
            "seed_count": 0,
        }
    limit = min(max(int(payload.get("limit", 24)), 1), 60)
    candidates, errors = [], []
    run_id = _services().clean_text(payload.get("run_id", ""), 80)
    step_id = _services().clean_text(payload.get("step_id", ""), 80)
    if run_id and step_id:
        _services()._agent_update_step(
            run_id,
            step_id,
            progress_current=0,
            progress_total=len(seed_items),
            message=f"并行扩展 {len(seed_items)} 个引用种子；每个种子最多 3 次 OpenAlex 请求",
        )

    def expand_seed(seed):
        seed_id, paper, local_id = seed
        try:
            if local_id:
                # Evidence-review runs should spend their latency budget
                # on the local PDF when one is already available. Citation
                # expansion is useful for discovery, but it is optional
                # once page-level evidence can be checked immediately.
                with _services().DB_LOCK, _services().db_session() as db:
                    has_local_fulltext = bool(
                        db.execute(
                            "SELECT 1 FROM attachments WHERE paper_id=? LIMIT 1",
                            (local_id,),
                        ).fetchone()
                    )
                if has_local_fulltext:
                    return seed_id, [], ""
                # Agent citation discovery is a bounded recall lane.  The
                # optional forward-citation endpoint can hold an entire
                # evidence review behind a slow provider, so keep the
                # primary work plus references/related works and let the
                # normal interactive expansion provide the full graph.
                try:
                    expanded = _services().expand_openalex_relations(
                        local_id,
                        limit=max(1, limit // len(seed_items)),
                        persist=False,
                        request_timeout=4,
                        include_cited_by=False,
                    )
                except TypeError:
                    # Preserve compatibility with test doubles and older
                    # connector adapters that expose the legacy signature.
                    expanded = _services().expand_openalex_relations(
                        local_id, max(1, limit // len(seed_items)), persist=False
                    )
            else:
                work_url, _ = _services()._openalex_work_reference(paper)
                try:
                    expanded = _services()._expand_openalex_relation_work(
                        work_url,
                        limit=max(1, limit // len(seed_items)),
                        persist=False,
                        request_timeout=4,
                        include_cited_by=False,
                    )
                except TypeError:
                    expanded = _services()._expand_openalex_relation_work(
                        work_url, max(1, limit // len(seed_items)), persist=False
                    )
            return seed_id, expanded.get("candidates", []), ""
        except Exception as exc:
            return seed_id, [], _services().clean_text(str(exc), 300)

    completed_seeds = {}
    with _services().ThreadPoolExecutor(
        max_workers=min(3, len(seed_items)), thread_name_prefix="agent-citation"
    ) as pool:
        futures = [pool.submit(expand_seed, seed) for seed in seed_items]
        for future in _services().as_completed(futures):
            seed_id, seed_candidates, error = future.result()
            completed_seeds[seed_id] = (seed_candidates, error)
            if error:
                errors.append({"seed_paper_id": seed_id, "error": error})
            if run_id and step_id:
                _services()._agent_update_step(
                    run_id,
                    step_id,
                    progress_current=len(completed_seeds),
                    progress_total=len(seed_items),
                    message=f"引用扩展完成 {len(completed_seeds)}/{len(seed_items)} 个种子",
                )
    for seed_id, _, _ in seed_items:
        seed_candidates, _ = completed_seeds.get(seed_id, ([], ""))
        for item in seed_candidates:
            record = _services()._agent_reduced_paper(item)
            record["relation"] = item.get("relation", "")
            record["seed_paper_id"] = seed_id
            record["id"] = (
                "candidate:citation:"
                + _services()
                .hashlib.sha1(
                    _services().candidate_identity(record).encode("utf-8")
                )
                .hexdigest()[:20]
            )
            candidates.append(record)
    candidates = _services()._unique_agent_papers(candidates, limit=60)
    history = {
        "new": [],
        "changed": [],
        "repeated": [],
        "new_count": 0,
        "changed_count": 0,
        "repeated_count": 0,
    }
    goal = payload.get("goal") or {}
    if goal.get("id") and candidates:
        with _services().DB_LOCK, _services().db_session() as db:
            goal_id = _services().clean_text(goal.get("id"), 80)
            if db.execute(
                "SELECT 1 FROM research_goals WHERE id=?", (goal_id,)
            ).fetchone():
                history = _services()._record_goal_paper_history(
                    db,
                    goal_id,
                    _services().clean_text(payload.get("run_id", ""), 80),
                    candidates,
                )
    return {
        "status": (
            "completed" if candidates else ("skipped" if not errors else "partial")
        ),
        "expanded": bool(candidates),
        "papers": candidates,
        "candidate_ids": [item.get("id") for item in candidates],
        "seed_count": len(seed_items),
        "history": history,
        "errors": errors,
        "warning": "引用扩展只返回候选快照，不会自动写入文献库",
    }


__all__ = ['_tool_prepare_fulltext', '_tool_download_fulltext', '_tool_extract_pdf_pages', '_tool_ocr_pdf_pages', '_tool_render_pdf_page', '_tool_search_document', '_tool_expand_citations']
