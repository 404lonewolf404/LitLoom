"""Paper record updates, revisions, reading cards, and source provenance."""

from __future__ import annotations

import sqlite3

from ..services import app_services as _services

def paper_history(paper_id: str, limit: int = 100) -> list[dict]:
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT id,paper_id,revision_type,content_json,source,created_at FROM paper_revisions WHERE paper_id=? ORDER BY id DESC LIMIT ?",
            (paper_id, min(max(int(limit), 1), 500)),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["content"] = _services().json.loads(item.pop("content_json"))
        except _services().json.JSONDecodeError:
            item["content"] = {}
        result.append(item)
    return result


def reading_card_dict(row: sqlite3.Row | dict | None, paper_id: str) -> dict:
    item = {field: "" for field in _services().READING_CARD_FIELDS}
    item.update({"paper_id": paper_id, "created_at": "", "updated_at": ""})
    if row:
        values = dict(row)
        for field in (*_services().READING_CARD_FIELDS, "created_at", "updated_at"):
            if field in values:
                item[field] = values[field] or ""
    item["completed_fields"] = sum(
        bool(item[field].strip()) for field in _services().READING_CARD_FIELDS
    )
    item["field_count"] = len(_services().READING_CARD_FIELDS)
    item["completion_pct"] = round(
        item["completed_fields"] / len(_services().READING_CARD_FIELDS) * 100
    )
    return item


def record_paper_revision(
    db: sqlite3.Connection,
    paper_id: str,
    revision_type: str,
    content: dict,
    source: str = "user",
) -> None:
    serialized = _services().json.dumps(content, ensure_ascii=False, sort_keys=True)
    latest = db.execute(
        "SELECT content_json FROM paper_revisions WHERE paper_id=? AND revision_type=? ORDER BY id DESC LIMIT 1",
        (paper_id, revision_type),
    ).fetchone()
    if latest and latest[0] == serialized:
        return
    db.execute(
        "INSERT INTO paper_revisions(paper_id,revision_type,content_json,source,created_at) VALUES(?,?,?,?,?)",
        (
            paper_id,
            revision_type,
            serialized,
            _services().clean_text(source, 40) or "user",
            _services().now_iso(),
        ),
    )


def save_reading_card(
    db: sqlite3.Connection, paper_id: str, data: dict, source: str = "user"
) -> dict:
    values = {
        field: _services().clean_text(data.get(field, ""), 20000)
        for field in _services().READING_CARD_FIELDS
    }
    _services().record_paper_revision(db, paper_id, "reading_card", values, source)
    now = _services().now_iso()
    columns = ",".join(_services().READING_CARD_FIELDS)
    placeholders = ",".join("?" for _ in _services().READING_CARD_FIELDS)
    assignments = ",".join(
        f"{field}=excluded.{field}" for field in _services().READING_CARD_FIELDS
    )
    db.execute(
        f"INSERT INTO reading_cards(paper_id,{columns},created_at,updated_at) VALUES(?,{placeholders},?,?) "
        f"ON CONFLICT(paper_id) DO UPDATE SET {assignments},updated_at=excluded.updated_at",
        (paper_id, *values.values(), now, now),
    )
    return _services().reading_card_dict(
        db.execute(
            "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
        ).fetchone(),
        paper_id,
    )


def batch_update_papers(ids, updates: dict) -> dict:
    if not isinstance(ids, list):
        raise ValueError("ids 必须是论文 ID 列表")
    if not 1 <= len(ids) <= 500:
        raise ValueError("每次批量操作需选择 1 到 500 篇论文")
    paper_ids = list(
        dict.fromkeys(
            _services().clean_text(item, 200)
            for item in ids
            if _services().clean_text(item, 200)
        )
    )
    if not paper_ids:
        raise ValueError("请至少选择一篇论文")
    if not isinstance(updates, dict):
        raise ValueError("updates 必须是对象")
    allowed = {"status", "topic", "is_favorite", "tags"}
    normalized = {key: value for key, value in updates.items() if key in allowed}
    if not normalized:
        raise ValueError("没有可更新的字段")
    if (
        "status" in normalized
        and normalized["status"] not in _services().PAPER_STATUSES
    ):
        raise ValueError("未知阅读状态")
    if "topic" in normalized:
        normalized["topic"] = _services().clean_text(normalized["topic"], 300)
    if "is_favorite" in normalized:
        favorite = normalized["is_favorite"]
        if favorite not in (True, False, 0, 1):
            raise ValueError("收藏状态必须是布尔值")
        normalized["is_favorite"] = int(bool(favorite))
    if "tags" in normalized:
        tags = normalized["tags"]
        if isinstance(tags, str):
            tags = [part.strip() for part in tags.split(",") if part.strip()]
        if not isinstance(tags, list):
            raise ValueError("标签必须是列表或逗号分隔文本")
        normalized["tags_json"] = _services().json.dumps(
            list(
                dict.fromkeys(
                    _services().clean_text(tag, 80)
                    for tag in tags
                    if _services().clean_text(tag, 80)
                )
            )[:50],
            ensure_ascii=False,
        )
        normalized.pop("tags")
    assignments = ",".join(f"{key}=?" for key in normalized)
    values = list(normalized.values())
    now = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        placeholders = ",".join("?" for _ in paper_ids)
        existing = {
            row[0]
            for row in db.execute(
                f"SELECT id FROM papers WHERE id IN ({placeholders})", paper_ids
            )
        }
        skipped = [paper_id for paper_id in paper_ids if paper_id not in existing]
        updated_ids = [paper_id for paper_id in paper_ids if paper_id in existing]
        if updated_ids:
            update_placeholders = ",".join("?" for _ in updated_ids)
            db.execute(
                f"UPDATE papers SET {assignments},updated_at=? WHERE id IN ({update_placeholders})",
                (*values, now, *updated_ids),
            )
            _services().record_audit(
                db,
                "papers.batch_updated",
                "paper",
                "",
                {
                    "selected": len(paper_ids),
                    "updated": len(updated_ids),
                    "skipped": len(skipped),
                    "fields": sorted(updates.keys()),
                },
            )
    return {"updated": len(updated_ids), "skipped": skipped}


def delete_papers(ids, *, protect_favorites: bool = False) -> dict:
    """Permanently remove selected library papers and their paper-owned data.

    Search runs and their candidate history remain available for auditability;
    records that point at a deleted local paper are detached. Attachment files
    are removed only when no remaining paper references the same content hash.
    """
    if not isinstance(ids, list):
        raise ValueError("ids 必须是论文 ID 列表")
    if not 1 <= len(ids) <= 5000:
        raise ValueError("每次批量删除需选择 1 到 5000 篇论文")
    paper_ids = list(
        dict.fromkeys(
            _services().clean_text(item, 200)
            for item in ids
            if _services().clean_text(item, 200)
        )
    )
    if not paper_ids:
        raise ValueError("请至少选择一篇论文")

    removed = []
    protected = []
    skipped = []
    attachment_files = []
    now = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        marks = ",".join("?" for _ in paper_ids)
        rows = db.execute(
            f"SELECT id,title,is_favorite FROM papers WHERE id IN ({marks})", paper_ids
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        for paper_id in paper_ids:
            row = by_id.get(paper_id)
            if not row:
                skipped.append(paper_id)
                continue
            if protect_favorites and bool(row["is_favorite"]):
                protected.append(paper_id)
                continue
            attachment_files.extend(
                dict(item)
                for item in db.execute(
                    "SELECT id,stored_name,sha256 FROM attachments WHERE paper_id=?",
                    (paper_id,),
                ).fetchall()
            )
            # These tables intentionally keep long-lived research history but
            # must not retain a live link to a paper that no longer exists.
            db.execute(
                "UPDATE discovery_candidates SET imported_paper_id='' WHERE imported_paper_id=?",
                (paper_id,),
            )
            db.execute(
                "UPDATE research_goal_papers SET local_paper_id='' WHERE local_paper_id=?",
                (paper_id,),
            )
            db.execute("DELETE FROM research_claims WHERE paper_id=?", (paper_id,))
            db.execute("DELETE FROM evidence_spans WHERE paper_id=?", (paper_id,))
            db.execute(
                "UPDATE ai_jobs SET cancel_requested=1,status='cancelled',stage='cancelled',message='所属论文已删除',updated_at=?,completed_at=? WHERE paper_id=? AND status IN ('queued','running')",
                (now, now, paper_id),
            )
            db.execute(
                "UPDATE semantic_index_jobs SET cancel_requested=1,status='cancelled',stage='cancelled',message='所属论文已删除',updated_at=?,completed_at=? WHERE paper_id=? AND status IN ('queued','running')",
                (now, now, paper_id),
            )
            cur = db.execute("DELETE FROM papers WHERE id=?", (paper_id,))
            if cur.rowcount:
                removed.append({"id": paper_id, "title": row["title"]})
        if removed:
            _services().record_audit(
                db,
                "papers.deleted",
                "paper",
                "",
                {
                    "deleted": len(removed),
                    "protected_favorites": len(protected),
                    "skipped": len(skipped),
                    "protect_favorites": bool(protect_favorites),
                },
            )

    # Files are outside SQLite's transaction. Remove only unreferenced files;
    # a failed cleanup is harmless and can be reported by maintenance status.
    for attachment in attachment_files:
        stored_name = _services().Path(str(attachment.get("stored_name", ""))).name
        if not stored_name:
            continue
        try:
            with _services().DB_LOCK, _services().db_session() as db:
                still_used = db.execute(
                    "SELECT 1 FROM attachments WHERE stored_name=? LIMIT 1",
                    (stored_name,),
                ).fetchone()
            if still_used:
                continue
            target = _services().active_files_dir() / stored_name
            if target.exists() and target.is_file():
                target.unlink()
            digest = _services().clean_text(attachment.get("sha256", ""), 64)
            prefixes = {
                digest,
                _services().clean_text(attachment.get("id", ""), 100),
            } - {""}
            if _services().active_visuals_dir().exists():
                for prefix in prefixes:
                    for image in (
                        _services().active_visuals_dir().glob(prefix + "-*.jpg")
                    ):
                        try:
                            image.unlink()
                        except OSError:
                            pass
        except OSError:
            # Keep the database deletion successful; diagnostics can expose
            # any orphaned file for a later maintenance cleanup.
            continue
    _services().invalidate_derived_cache()
    return {
        "deleted": len(removed),
        "deleted_paper_ids": [item["id"] for item in removed],
        "protected": protected,
        "skipped": skipped,
        "attachments_considered": len(attachment_files),
    }


def attach_source(
    db,
    paper_id: str,
    source: str,
    external_id: str = "",
    query_id: str = "",
    raw: dict | None = None,
):
    db.execute(
        """INSERT INTO source_records(paper_id,source,external_id,query_id,fetched_at,raw_json)
                  VALUES(?,?,?,?,?,?)
                  ON CONFLICT(paper_id,source,external_id,query_id) DO UPDATE SET fetched_at=excluded.fetched_at,raw_json=excluded.raw_json""",
        (
            paper_id,
            source,
            external_id,
            query_id,
            _services().now_iso(),
            _services().json.dumps(raw or {}, ensure_ascii=False)[:60000],
        ),
    )


def find_existing_paper(db, record: dict):
    """Find the library row that would be merged by ``upsert_paper``."""
    paper = _services().normalize_record(record)
    if not paper["title"]:
        return None
    found = None
    if paper["doi"]:
        found = db.execute(
            "SELECT * FROM papers WHERE doi=? LIMIT 1", (paper["doi"],)
        ).fetchone()
    if not found and paper["arxiv_id"]:
        found = db.execute(
            "SELECT * FROM papers WHERE arxiv_id=? LIMIT 1", (paper["arxiv_id"],)
        ).fetchone()
    if not found and not paper["doi"] and not paper["arxiv_id"] and paper["year"]:
        found = db.execute(
            "SELECT * FROM papers WHERE title_key=? AND year=? LIMIT 1",
            (paper["title_key"], paper["year"]),
        ).fetchone()
        if not found:
            # Require a very high title overlap and the same year when no
            # stronger identifier is available.
            for candidate in db.execute(
                "SELECT * FROM papers WHERE year=? ORDER BY updated_at DESC LIMIT 200",
                (paper["year"],),
            ).fetchall():
                if _services().title_similarity(paper["title"], candidate["title"]) >= 0.92:
                    found = candidate
                    break
    return found


def upsert_paper(
    record: dict,
    source: str = "manual",
    external_id: str = "",
    query_id: str = "",
    raw: dict | None = None,
) -> tuple[str, bool]:
    p = _services().normalize_record(record)
    if not p["title"]:
        raise ValueError("论文标题不能为空")
    with _services().DB_LOCK, _services().db_session() as db:
        found = _services().find_existing_paper(db, p)
        if found:
            pid = found["id"]
            # Fill gaps in bibliographic fields without overwriting user notes/status/topic.
            for field in (
                "authors",
                "abstract",
                "doi",
                "arxiv_id",
                "venue",
                "url",
                "pdf_url",
            ):
                if p[field] and not found[field]:
                    db.execute(
                        f"UPDATE papers SET {field}=?,updated_at=? WHERE id=?",
                        (p[field], _services().now_iso(), pid),
                    )
            _services().attach_source(db, pid, source, external_id, query_id, raw)
            _services().record_audit(
                db,
                "paper.merged",
                "paper",
                pid,
                {"source": source, "query_id": query_id, "external_id": external_id},
            )
            _services().invalidate_derived_cache()
            return pid, False
        pid = str(_services().uuid.uuid4())
        cols = ["id", *p.keys(), "created_at", "updated_at"]
        vals = [pid, *p.values(), _services().now_iso(), _services().now_iso()]
        db.execute(
            f"INSERT INTO papers({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
            vals,
        )
        _services().attach_source(
            db, pid, source, external_id or p["doi"] or p["arxiv_id"], query_id, raw
        )
        _services().record_audit(
            db, "paper.created", "paper", pid, {"source": source, "query_id": query_id}
        )
        _services().invalidate_derived_cache()
        return pid, True


__all__ = ['paper_history', 'reading_card_dict', 'record_paper_revision', 'save_reading_card', 'batch_update_papers', 'delete_papers', 'attach_source', 'find_existing_paper', 'upsert_paper']
