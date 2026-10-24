"""Background sync-job snapshots and research briefings."""

from __future__ import annotations

from ..services import app_services as _services


def sync_job_snapshot(row) -> dict:
    """Return a durable, UI-safe snapshot for one literature sync job.

    ``sync_jobs`` is intentionally small and records the scheduler state,
    while the corresponding ``runs`` row contains the user-facing name and
    result counters.  Joining those records here keeps every client (including
    a freshly reloaded browser) from having to reconstruct a run and prevents
    internal UUIDs from being shown as the research topic.
    """
    if not row:
        return {}
    item = dict(row)
    # The run row is the source of truth for terminal state. A scheduler row
    # can lag briefly, which otherwise makes a finished search look active.
    run_status = _services().clean_text(item.pop("run_status", ""), 40)
    if run_status:
        item["status"] = run_status
    raw_sources = item.pop("run_sources_json", item.pop("sources_json", "[]")) or "[]"
    try:
        sources = _services().json.loads(raw_sources)
    except (TypeError, ValueError):
        sources = []
    if not isinstance(sources, list):
        sources = []
    sources = [
        _services().clean_text(value, 80)
        for value in sources
        if _services().clean_text(value, 80)
    ]
    item["sources"] = sources
    item.pop("sources_json", None)

    query_name = _services().clean_text(item.pop("run_query_name", ""), 500)
    query_name = query_name or _services().clean_text(
        item.pop("saved_query_name", ""), 500
    )
    item["query_name"] = (
        query_name
        or _services().clean_text(item.get("query_id", ""), 120)
        or "研究主题"
    )
    item["query"] = _services().clean_text(
        item.pop("saved_query", "") or item.pop("run_query", ""), 1000
    )
    item["run_started_at"] = item.pop("run_started_at_raw", "") or item.get(
        "created_at", ""
    )
    item["run_completed_at"] = item.pop("run_completed_at_raw", "")
    item["run_error"] = _services().clean_text(item.pop("run_error_raw", ""), 1000)
    item["new_count"] = int(item.pop("run_new_count", 0) or 0)
    item["duplicate_count"] = int(item.pop("run_duplicate_count", 0) or 0)

    summary = _services()._json_object(item.pop("run_summary_json", "{}"))
    visible_ids = summary.get("candidate_result_ids")
    legacy_results = summary.get("candidate_results")
    stored_count = int(item.pop("run_candidate_count", 0) or 0)
    item["candidate_mode"] = bool(summary.get("candidate_mode"))
    item["candidate_count"] = (
        len(visible_ids)
        if isinstance(visible_ids, list)
        else len(legacy_results) if isinstance(legacy_results, list) else stored_count
    )
    item["imported_count"] = int(item.pop("run_imported_count", 0) or 0)
    item["awaiting_review_count"] = max(
        0, item["candidate_count"] - item["imported_count"]
    )

    # ``source_details_json`` is written when a run finishes.  It gives the
    # task center useful totals after completion without exposing the raw run
    # record as an implementation detail.
    raw_details = item.pop("run_source_details_json", "[]") or "[]"
    try:
        details = _services().json.loads(raw_details)
    except (TypeError, ValueError):
        details = []
    if not isinstance(details, list):
        details = []
    item["source_details"] = details
    # Model-led runs can be created through the Agent/MCP entry point with no
    # user-selected connector list, so ``runs.sources_json`` is legitimately
    # empty even though the model search has a durable source detail. Prefer
    # the completed detail list whenever it is available; this keeps a reload
    # from showing an impossible 0/0 source progress. For an active model
    # search, expose its single logical source before detail rows are written.
    detail_sources = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        source_name = _services().clean_text(detail.get("source"), 80)
        if source_name and source_name not in detail_sources:
            detail_sources.append(source_name)
    stage_hint = str(item.get("stage") or "")
    if detail_sources and (not sources or "model_web" in detail_sources):
        sources = detail_sources
    elif not sources and (
        stage_hint == "model_web_search" or stage_hint.startswith("model_web_search:")
    ):
        sources = ["model_web"]
    item["sources"] = sources
    item["total_fetched"] = sum(
        int(detail.get("fetched") or 0)
        for detail in details
        if isinstance(detail, dict)
    )
    item["total_filtered"] = sum(
        int(detail.get("filtered") or 0)
        for detail in details
        if isinstance(detail, dict)
    )

    stage = stage_hint
    current_source = ""
    source_index = 0
    if stage.startswith("model_web_search") and "model_web" in sources:
        current_source = "model_web"
        source_index = sources.index("model_web") + 1
    elif stage.startswith("source:"):
        current_source = stage.split(":", 1)[1]
        if current_source in sources:
            source_index = sources.index(current_source) + 1
    elif item.get("status") in {"success", "partial", "failed", "interrupted"}:
        source_index = len(sources)
    item["current_source"] = current_source
    item["source_index"] = source_index
    item["source_total"] = len(sources)
    item["progress_current"] = source_index
    item["progress_total"] = len(sources)

    started = item.get("run_started_at") or item.get("created_at")
    ended = item.get("run_completed_at") or (
        _services().now_iso()
        if item.get("status") in {"running", "queued"}
        else started
    )
    try:
        item["duration_ms"] = max(
            0,
            round(
                (
                    _services().datetime.fromisoformat(ended)
                    - _services().datetime.fromisoformat(started)
                ).total_seconds()
                * 1000,
                2,
            ),
        )
    except (TypeError, ValueError):
        item["duration_ms"] = 0
    return item


def research_briefing(days: int = 7, limit: int = 8) -> dict:
    days = min(max(int(days), 1), 90)
    limit = min(max(int(limit), 1), 20)
    return _services().cached_derived(
        f"research_briefing:{days}:{limit}:{_services().database_change_token()}",
        lambda: _services()._research_briefing(days, limit),
    )


def _research_briefing(days: int = 7, limit: int = 8) -> dict:
    """Build a local, reproducible research brief for the current window."""
    days = min(max(int(days), 1), 90)
    limit = min(max(int(limit), 1), 20)
    now = _services().datetime.now(_services().timezone.utc).replace(microsecond=0)
    horizon = (now - _services().timedelta(days=days)).isoformat()
    with _services().DB_LOCK, _services().db_session() as db:
        new_rows = [
            _services().paper_dict(row)
            for row in db.execute(
                "SELECT * FROM papers WHERE created_at>=? AND status<>'excluded' ORDER BY created_at DESC LIMIT 200",
                (horizon,),
            )
        ]
        status_rows = [
            dict(row)
            for row in db.execute(
                "SELECT status,COUNT(*) AS n FROM papers GROUP BY status"
            )
        ]
        query_rows = [
            dict(row)
            for row in db.execute(
                "SELECT id,name,query,active,last_run_at,next_run_at FROM saved_queries ORDER BY active DESC,updated_at DESC"
            )
        ]
        run_rows = [
            dict(row)
            for row in db.execute(
                "SELECT status,new_count,duplicate_count,started_at,completed_at FROM runs WHERE started_at>=? ORDER BY started_at DESC",
                (horizon,),
            )
        ]
        source_rows = [
            dict(row)
            for row in db.execute(
                "SELECT sr.source,COUNT(*) AS n FROM source_records sr JOIN papers p ON p.id=sr.paper_id WHERE sr.fetched_at>=? GROUP BY sr.source ORDER BY n DESC",
                (horizon,),
            )
        ]
    topics = {}
    for paper in new_rows:
        topic = paper.get("topic") or "未分类"
        topics[topic] = topics.get(topic, 0) + 1
    compact = lambda paper: {
        key: paper.get(key, "")
        for key in (
            "id",
            "title",
            "authors",
            "year",
            "venue",
            "abstract",
            "status",
            "topic",
            "url",
            "pdf_url",
            "doi",
            "arxiv_id",
            "created_at",
            "updated_at",
        )
    }
    local_memory = _services().cached_derived(
        f"research_memory:base:{_services().database_change_token()}",
        lambda: _services()._research_memory(50),
        ttl_seconds=30.0,
    )
    recommendations = list(local_memory.get("recommendations", []))[:limit]
    failures = sum(1 for row in run_rows if row.get("status") in {"failed", "partial"})
    active_topics = sum(1 for row in query_rows if row.get("active"))
    status_map = {row["status"]: int(row["n"]) for row in status_rows}
    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "summary": {
            "new_papers": len(new_rows),
            "backlog": sum(
                status_map.get(status, 0) for status in ("inbox", "to_read", "later")
            ),
            "reading": status_map.get("reading", 0),
            "read": status_map.get("read", 0),
            "active_topics": active_topics,
            "sync_runs": len(run_rows),
            "partial_or_failed_runs": failures,
        },
        "topic_breakdown": [
            {"topic": topic, "count": count}
            for topic, count in sorted(
                topics.items(), key=lambda pair: (-pair[1], pair[0])
            )[:12]
        ],
        "source_activity": source_rows,
        "recent_papers": [compact(paper) for paper in new_rows[:limit]],
        "next_reads": [
            compact(paper)
            | {
                "recommendation_score": paper.get("recommendation_score"),
                "recommendation_reasons": paper.get("recommendation_reasons", []),
            }
            for paper in recommendations
        ],
        "attention": (
            ["有来源运行失败或部分失败，请先检查来源健康"] if failures else []
        )
        + (
            ["待读积压超过新增量，建议先完成分诊"]
            if status_map.get("to_read", 0) > len(new_rows)
            and status_map.get("to_read", 0) > 10
            else []
        ),
    }


__all__ = ["sync_job_snapshot", "research_briefing", "_research_briefing"]
