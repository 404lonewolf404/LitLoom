"""Paper detail, local library search, and search-index operations."""

from __future__ import annotations

import sqlite3

from ..services import app_services as _services

def paper_connections(paper_id: str) -> dict:
    """Explain how one paper relates to saved topics and the local library."""
    with _services().DB_LOCK, _services().db_session() as db:
        paper_row = db.execute(
            "SELECT * FROM papers WHERE id=?", (paper_id,)
        ).fetchone()
        if not paper_row:
            raise ValueError("论文不存在")
        paper = _services().paper_dict(paper_row)
        queries = [
            dict(row)
            for row in db.execute(
                "SELECT id,name,query,active FROM saved_queries ORDER BY active DESC,updated_at DESC"
            )
        ]
        others = [
            _services().paper_dict(row)
            for row in db.execute(
                "SELECT * FROM papers WHERE id<>? AND status<> 'excluded' ORDER BY updated_at DESC",
                (paper_id,),
            )
        ]
    topics = []
    for query in queries:
        result = _services().text_overlap(
            _services().paper_text(paper), query.get("query", "")
        )
        reasons = (
            [f"命中词：{'、'.join(result['hits'][:6])}"]
            if result["hits"]
            else ["没有共享的明确词项"]
        )
        topics.append(
            {
                "id": query["id"],
                "name": query["name"] or query["query"],
                "active": bool(query["active"]),
                "score": result["score"],
                "tier": _services().connection_tier(result["score"]),
                "reasons": reasons,
            }
        )
    topics.sort(key=lambda item: (-item["score"], item["name"]))
    related = []
    for other in others:
        result = _services().text_overlap(
            _services().paper_text(paper), _services().paper_text(other)
        )
        if result["score"] < 10:
            continue
        related.append(
            {
                "id": other["id"],
                "title": other["title"],
                "year": other.get("year"),
                "status": other.get("status"),
                "topic": other.get("topic", ""),
                "score": result["score"],
                "tier": _services().connection_tier(result["score"]),
                "hits": result["hits"],
            }
        )
    related.sort(
        key=lambda item: (-item["score"], item.get("year") or 0, item["title"])
    )
    return {
        "paper_id": paper_id,
        "topics": topics,
        "related": related[:12],
        "generated_at": _services().now_iso(),
    }


def paper_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["tags"] = _services().json.loads(item.pop("tags_json", "[]"))
    item["is_favorite"] = bool(item["is_favorite"])
    return item


def _hydrate_retrieval_metadata(paper: dict, source: str = "legacy_snapshot") -> dict:
    """Expose the explicit retrieval verdict for old saved search records.

    Before the retrieval/AI-screen split was introduced, candidate snapshots
    only carried the legacy ``relevance_*`` (or ``discovery_*``) fields.  The
    UI can still render those fields, but returning the explicit contract here
    keeps refreshed history consistent with newly created searches.
    """
    if not isinstance(paper, dict):
        return paper
    if paper.get("retrieval_relevance_score") in (None, ""):
        score = paper.get("relevance_score")
        if score in (None, ""):
            score = paper.get("discovery_score")
        if score not in (None, ""):
            paper["retrieval_relevance_score"] = score
    if not paper.get("retrieval_relevance_label"):
        label = paper.get("relevance_label") or paper.get("discovery_class")
        if label:
            paper["retrieval_relevance_label"] = label
    if not paper.get("retrieval_relevance_reasons"):
        reasons = paper.get("relevance_reasons") or paper.get("discovery_reasons") or []
        paper["retrieval_relevance_reasons"] = (
            list(reasons) if isinstance(reasons, (list, tuple)) else [str(reasons)]
        )
    if not paper.get("retrieval_relevance_source"):
        paper["retrieval_relevance_source"] = source
    return paper


def paper_detail(paper_id: str, include_ai_artifacts: bool = True) -> dict | None:
    """Return one paper and its durable metadata.

    The browser opens the detail record before it separately requests the
    analysis history.  Returning several potentially multi-megabyte model
    answers in that first request made reopening a paper unnecessarily slow
    and increased SQLite lock time while a background job was writing.  Keep
    the full payload as the backwards-compatible default for API callers, but
    allow the UI to request metadata-only artifact rows.
    """
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row:
            return None
        item = _services().paper_dict(row)
        item["reading_card"] = _services().reading_card_dict(
            db.execute(
                "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
            ).fetchone(),
            paper_id,
        )
        item["sources"] = [
            dict(r)
            for r in db.execute(
                "SELECT source,external_id,fetched_at FROM source_records WHERE paper_id=? ORDER BY fetched_at DESC",
                (paper_id,),
            )
        ]
        item["attachments"] = [
            dict(r)
            for r in db.execute(
                "SELECT id,original_name,stored_name,sha256,byte_size,added_at FROM attachments WHERE paper_id=?",
                (paper_id,),
            )
        ]
        item["has_extracted_pages"] = bool(
            db.execute(
                "SELECT 1 FROM document_pages WHERE paper_id=? LIMIT 1", (paper_id,)
            ).fetchone()
        )
        artifact_columns = (
            "id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at"
            if include_ai_artifacts
            else "id,artifact_type,provider,model,prompt_version,input_sha256,created_at"
        )
        artifact_rows = [
            dict(r)
            for r in db.execute(
                f"SELECT {artifact_columns} FROM ai_artifacts WHERE paper_id=? ORDER BY created_at DESC LIMIT 5",
                (paper_id,),
            )
        ]
        # Keep the latest abstract brief discoverable even after many full-text
        # analyses have been generated and the mixed five-item window moves on.
        if not any(
            row.get("artifact_type") == "abstract_brief" for row in artifact_rows
        ):
            latest_brief = db.execute(
                f"SELECT {artifact_columns} FROM ai_artifacts WHERE paper_id=? AND artifact_type='abstract_brief' ORDER BY created_at DESC LIMIT 1",
                (paper_id,),
            ).fetchone()
            if latest_brief:
                artifact_rows.append(dict(latest_brief))
                artifact_rows.sort(
                    key=lambda row: row.get("created_at", ""), reverse=True
                )
        item["ai_artifacts"] = artifact_rows
    item["has_local_pdf"] = any(
        bool(attachment.get("stored_name"))
        and (
            _services().active_files_dir()
            / _services().Path(str(attachment["stored_name"])).name
        ).is_file()
        for attachment in item["attachments"]
    )
    item["has_fulltext_source"] = item["has_local_pdf"] or item["has_extracted_pages"]
    item.pop("has_extracted_pages", None)
    return item


def refresh_search_content(db: sqlite3.Connection, paper_id: str) -> None:
    """Refresh one paper's derived search document inside the caller transaction."""
    row = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not row:
        db.execute("DELETE FROM paper_search_content WHERE paper_id=?", (paper_id,))
        return
    card = db.execute(
        "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
    ).fetchone()
    card_text = (
        " ".join(str(card[field] or "") for field in _services().READING_CARD_FIELDS)
        if card
        else ""
    )
    try:
        tags = " ".join(_services().json.loads(row["tags_json"] or "[]"))
    except (TypeError, _services().json.JSONDecodeError):
        tags = row["tags_json"] or ""
    db.execute(
        """INSERT INTO paper_search_content(paper_id,title,authors,abstract,topic,tags,notes,reading_card,pdf_text)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(paper_id) DO UPDATE SET title=excluded.title,authors=excluded.authors,abstract=excluded.abstract,
        topic=excluded.topic,tags=excluded.tags,notes=excluded.notes,reading_card=excluded.reading_card,pdf_text=excluded.pdf_text""",
        (
            paper_id,
            row["title"] or "",
            row["authors"] or "",
            row["abstract"] or "",
            row["topic"] or "",
            tags,
            row["notes"] or "",
            card_text,
            "",
        ),
    )


def rebuild_search_index() -> dict:
    """Rebuild the derived local search index without changing source records."""
    started = _services().time.perf_counter()
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute("DELETE FROM paper_search_content")
        db.execute("DELETE FROM paper_page_search_content")
        for row in db.execute("SELECT id FROM papers ORDER BY id").fetchall():
            _services().refresh_search_content(db, row["id"])
        fts = _services().database_table_exists(db, "paper_search")
        page_fts = _services().database_table_exists(db, "paper_page_search")
        db.execute(
            "INSERT INTO paper_page_search_content(page_id,paper_id,text) SELECT id,paper_id,text FROM document_pages"
        )
        if fts:
            try:
                db.execute("INSERT INTO paper_search(paper_search) VALUES('rebuild')")
            except _services().sqlite3.OperationalError:
                fts = False
        if page_fts:
            try:
                db.execute(
                    "INSERT INTO paper_page_search(paper_page_search) VALUES('rebuild')"
                )
            except _services().sqlite3.OperationalError:
                page_fts = False
        count = db.execute("SELECT COUNT(*) FROM paper_search_content").fetchone()[0]
        page_count = db.execute(
            "SELECT COUNT(*) FROM paper_page_search_content"
        ).fetchone()[0]
        _services().record_audit(
            db,
            "maintenance.search_index_rebuilt",
            "search_index",
            "",
            {
                "documents": count,
                "page_documents": page_count,
                "fts5": fts and page_fts,
            },
        )
    return {
        "rebuilt": True,
        "documents": count,
        "page_documents": page_count,
        "fts5": fts and page_fts,
        "duration_ms": round((_services().time.perf_counter() - started) * 1000, 2),
        "completed_at": _services().now_iso(),
    }


def search_library(
    query: str,
    limit: int = 40,
    offset: int = 0,
    status: str = "",
    topic: str = "",
    filters: dict | None = None,
) -> dict:
    """Search papers and local reading evidence, returning snippets and match fields."""
    query = _services().clean_text(query, 500)
    limit = min(max(int(limit), 1), 200)
    offset = max(0, min(int(offset), 1000000))
    clauses, args = [], []
    filters = filters or {}
    if status:
        statuses = [
            _services().clean_text(value, 40)
            for value in str(status).split(",")
            if _services().clean_text(value, 40)
        ]
        if len(statuses) == 1:
            clauses.append("p.status=?")
            args.append(statuses[0])
        elif statuses:
            clauses.append("p.status IN (" + ",".join("?" for _ in statuses) + ")")
            args.extend(statuses)
    if topic:
        clauses.append("p.topic=?")
        args.append(topic)
    if filters.get("favorite"):
        clauses.append("p.is_favorite=1")
    if filters.get("has_reading_card"):
        clauses.append("EXISTS (SELECT 1 FROM reading_cards rc WHERE rc.paper_id=p.id)")
    for key, op in (("year_from", ">="), ("year_to", "<=")):
        try:
            year = int(filters.get(key)) if filters.get(key) not in (None, "") else None
        except (TypeError, ValueError):
            year = None
        if year:
            clauses.append(f"p.year{op}?")
            args.append(year)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _services().DB_LOCK, _services().db_session() as db:
        total_args = list(args)
        if query:
            fts = _services().database_table_exists(
                db, "paper_search"
            ) and _services().database_table_exists(db, "paper_page_search")
            ascii_terms = [
                term
                for term in _services().re.findall(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{1,}", query
                )
            ]
            use_fts = fts and bool(ascii_terms)
            if use_fts:
                match = " AND ".join(
                    f'"{term.replace(chr(34), "")}"*' for term in ascii_terms[:12]
                )
                # The content table is joined for stable paper ids; FTS is used
                # only to rank/select rowids because it stores the external content rowid.
                clauses_query = "(p.id IN (SELECT pc.paper_id FROM paper_search JOIN paper_search_content pc ON pc.rowid=paper_search.rowid WHERE paper_search MATCH ?) OR p.id IN (SELECT ppc.paper_id FROM paper_page_search JOIN paper_page_search_content ppc ON ppc.rowid=paper_page_search.rowid WHERE paper_page_search MATCH ?))"
                query_args = [match, match]
            else:
                clauses_query = "(p.title LIKE ? OR p.authors LIKE ? OR p.abstract LIKE ? OR p.topic LIKE ? OR p.notes LIKE ? OR p.doi LIKE ? OR ps.tags LIKE ? OR ps.reading_card LIKE ? OR EXISTS (SELECT 1 FROM document_pages dp WHERE dp.paper_id=p.id AND dp.text LIKE ?))"
                like = f"%{query}%"
                query_args = [like] * 9
            if clauses_query:
                clauses.append(clauses_query)
                args.extend(query_args)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        # Fallback queries need the content table join; FTS queries use a
        # correlated subquery and stay independent of FTS column layout.
        join = (
            " JOIN paper_search_content ps ON ps.paper_id=p.id"
            if query and not (fts and ascii_terms)
            else ""
        )
        total = db.execute(
            f"SELECT COUNT(*) FROM papers p{join}{where}", tuple(args)
        ).fetchone()[0]
        order_by = {
            "oldest": "p.updated_at ASC,p.id ASC",
            "year_desc": "p.year DESC,p.updated_at DESC",
            "year_asc": "p.year ASC,p.updated_at DESC",
            "title": "p.title COLLATE NOCASE ASC,p.id ASC",
        }.get(filters.get("sort"), "p.is_favorite DESC,p.updated_at DESC,p.id ASC")
        ranked_search = bool(
            query
            and fts
            and ascii_terms
            and filters.get("sort") not in {"oldest", "year_desc", "year_asc", "title"}
        )
        rank_select = (
            ", (SELECT MIN(score) FROM (SELECT bm25(paper_search) AS score FROM paper_search JOIN paper_search_content rank_pc ON rank_pc.rowid=paper_search.rowid WHERE paper_search MATCH ? AND rank_pc.paper_id=p.id UNION ALL SELECT bm25(paper_page_search) AS score FROM paper_page_search JOIN paper_page_search_content rank_ppc ON rank_ppc.rowid=paper_page_search.rowid WHERE paper_page_search MATCH ? AND rank_ppc.paper_id=p.id)) AS search_rank"
            if ranked_search
            else ""
        )
        page_select = (
            ", (SELECT snippet(paper_page_search,0,'','','…',30) FROM paper_page_search JOIN paper_page_search_content page_pc ON page_pc.rowid=paper_page_search.rowid WHERE paper_page_search MATCH ? AND page_pc.paper_id=p.id LIMIT 1) AS page_match"
            if query and fts and ascii_terms
            else (
                ", (SELECT substr(dp.text,1,2000) FROM document_pages dp WHERE dp.paper_id=p.id AND dp.text LIKE ? ORDER BY dp.page_number LIMIT 1) AS page_match"
                if query
                else ",'' AS page_match"
            )
        )
        if ranked_search:
            order_by = "p.is_favorite DESC,search_rank ASC,p.updated_at DESC,p.id ASC"
        rank_args = [match, match] if ranked_search else []
        page_args = (
            [match]
            if query and fts and ascii_terms
            else ([f"%{query}%"] if query else [])
        )
        row_args = rank_args + page_args + args
        rows = db.execute(
            f"SELECT p.*,ps.reading_card,ps.pdf_text{rank_select}{page_select} FROM papers p{join or ' LEFT JOIN paper_search_content ps ON ps.paper_id=p.id'}{where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (*row_args, limit, offset),
        ).fetchall()
        results = []
        ids = [row["id"] for row in rows]
        cards, sources, attachments, artifacts = {}, {}, {}, {}
        if ids:
            marks = ",".join("?" for _ in ids)
            cards = {
                row["paper_id"]: row
                for row in db.execute(
                    f"SELECT * FROM reading_cards WHERE paper_id IN ({marks})", ids
                )
            }
            for row in db.execute(
                f"SELECT paper_id,source,external_id,fetched_at FROM source_records WHERE paper_id IN ({marks}) ORDER BY fetched_at DESC",
                ids,
            ):
                sources.setdefault(row["paper_id"], []).append(dict(row))
            for row in db.execute(
                f"SELECT paper_id,id,original_name,stored_name,sha256,byte_size,added_at FROM attachments WHERE paper_id IN ({marks})",
                ids,
            ):
                attachments.setdefault(row["paper_id"], []).append(dict(row))
            for row in db.execute(
                f"SELECT paper_id,id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at FROM ai_artifacts WHERE paper_id IN ({marks}) ORDER BY created_at DESC",
                ids,
            ):
                artifacts.setdefault(row["paper_id"], []).append(dict(row))
        for row in rows:
            item = _services().paper_dict(row)
            item.pop("pdf_text", None)
            item["reading_card"] = _services().reading_card_dict(
                cards.get(item["id"]), item["id"]
            )
            item["sources"] = sources.get(item["id"], [])
            item["attachments"] = attachments.get(item["id"], [])
            item["ai_artifacts"] = artifacts.get(item["id"], [])[:5]
            hay = {
                "title": row["title"] or "",
                "authors": row["authors"] or "",
                "abstract": row["abstract"] or "",
                "topic": row["topic"] or "",
                "tags": row["tags_json"] or "",
                "notes": row["notes"] or "",
                "reading_card": row["reading_card"] or "",
                "pdf_text": row["page_match"] or "",
            }
            lower = query.casefold()
            fields = [
                name
                for name, value in hay.items()
                if lower and lower in value.casefold()
            ]
            if not fields and query:
                terms = [
                    term.casefold()
                    for term in _services().re.findall(
                        r"[\w-]{2,}", query, flags=_services().re.UNICODE
                    )
                ]
                fields = [
                    name
                    for name, value in hay.items()
                    if any(term in value.casefold() for term in terms)
                ]
            source = next(
                (
                    name
                    for name in (
                        "title",
                        "authors",
                        "abstract",
                        "topic",
                        "notes",
                        "reading_card",
                        "pdf_text",
                    )
                    if name in fields
                ),
                "",
            )
            value = hay.get(source, "")
            pos = value.casefold().find(lower) if lower else -1
            if pos < 0 and value:
                pos = 0
            item.update(
                {
                    "match_fields": fields,
                    "match_source": source,
                    "snippet": (
                        value[max(0, pos - 100) : pos + 260] if value else ""
                    ).strip(),
                }
            )
            results.append(item)
    return {
        "query": query,
        "results": results,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(results) < total,
        "generated_at": _services().now_iso(),
    }


__all__ = ['paper_connections', 'paper_dict', '_hydrate_retrieval_metadata', 'paper_detail', 'refresh_search_content', 'rebuild_search_index', 'search_library']
