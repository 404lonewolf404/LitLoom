"""Search run persistence and state transitions."""

from __future__ import annotations

from ..services import app_services as _services

def _persist_search_results(fetch_state) -> None:
    # Keep the exact ranked result set with its run so a page reload can
    # restore an ad-hoc search without issuing another network search.
    fetch_state.result_metadata_fields = (
        "discovery_score",
        "seen_before",
        "seen_count",
        "first_seen_at",
        "user_decision",
        "viewed_at",
        "suppressed",
        "source_count",
        "discovery_reasons",
        "relevance_reasons",
        "retrieval_relevance_score",
        "retrieval_relevance_label",
        "retrieval_relevance_reasons",
        "retrieval_relevance_source",
        "match_reason",
        "query_id",
        "open_access",
        "citation_count",
        "discovery_lanes",
        "rank_fusion_score",
        "rank_sources",
        "rank_paths",
        "novelty_score",
        "recency_score",
        "impact_score",
        "source_quality_score",
        "diversity_penalty",
        "diversity_score",
        "search_rank",
        "candidate_key",
        "intent_key",
        "prior_feedback",
        "review_decision",
        "selection_backfilled",
        "history_quality_accepted",
    )
    with _services().DB_LOCK, _services().db_session() as fetch_state.db:
        if not fetch_state.persist_results and fetch_state.late_candidate_records:
            fetch_state.db.executemany(
                "INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,candidate_key=excluded.candidate_key,record_json=excluded.record_json,created_at=excluded.created_at",
                [
                    (
                        fetch_state.candidate_id,
                        fetch_state.run_id,
                        _services().candidate_identity(fetch_state.record),
                        _services().json.dumps(fetch_state.record, ensure_ascii=False),
                        _services().now_iso(),
                    )
                    for fetch_state.candidate_id, fetch_state.record in fetch_state.late_candidate_records.items()
                ],
            )
        fetch_state.snapshot_summary = dict(fetch_state.discovery_summary)
        for fetch_state.observation in fetch_state.search_observations.values():
            _services().record_discovery_search_memory(
                fetch_state.db, fetch_state.intent_key, fetch_state.observation, fetch_state.run_id, _services().now_iso()
            )
        if not fetch_state.persist_results:
            fetch_state.snapshot_summary["candidate_mode"] = True
            fetch_state.snapshot_summary["candidate_results"] = fetch_state.results
            fetch_state.snapshot_summary["candidate_result_ids"] = [
                fetch_state.paper["id"] for fetch_state.paper in fetch_state.results if fetch_state.paper.get("id")
            ]
            fetch_state.snapshot_summary["intent_key"] = fetch_state.intent_key
            fetch_state.snapshot_summary["model_web_quality_filtered"] = fetch_state.model_web_quality_filtered
            # The candidate table is the mutable durable source of truth.
            # Persist final rank/feedback fields here so AI annotations and
            # later user decisions survive a page reload without relying on
            # the immutable initial result list in summary_json.
            fetch_state.db.executemany(
                "UPDATE discovery_candidates SET record_json=? WHERE run_id=? AND id=?",
                [
                    (
                        _services().json.dumps(fetch_state.paper, ensure_ascii=False),
                        fetch_state.run_id,
                        fetch_state.paper["id"],
                    )
                    for fetch_state.paper in fetch_state.results
                    if str(fetch_state.paper.get("id", "")).startswith("candidate:")
                ],
            )
            # Connector/model fetches are staged before ranking. Remove rows
            # rejected by the quality gate so history counts and retry/triage
            # state describe exactly what the user can review.
            fetch_state.visible_candidate_ids = [
                fetch_state.paper["id"]
                for fetch_state.paper in fetch_state.results
                if str(fetch_state.paper.get("id", "")).startswith("candidate:")
            ]
            if fetch_state.visible_candidate_ids:
                fetch_state.placeholders = ",".join("?" for _ in fetch_state.visible_candidate_ids)
                fetch_state.db.execute(
                    f"DELETE FROM discovery_candidates WHERE run_id=? AND id NOT IN ({fetch_state.placeholders})",
                    (fetch_state.run_id, *fetch_state.visible_candidate_ids),
                )
            else:
                fetch_state.db.execute("DELETE FROM discovery_candidates WHERE run_id=?", (fetch_state.run_id,))
        fetch_state.db.execute(
            "UPDATE run_snapshots SET plan_json=?,summary_json=? WHERE run_id=?",
            (
                _services().json.dumps(fetch_state.plan, ensure_ascii=False),
                _services().json.dumps(fetch_state.snapshot_summary, ensure_ascii=False),
                fetch_state.run_id,
            ),
        )
        if fetch_state.persist_results:
            fetch_state.db.executemany(
                "INSERT INTO run_papers(run_id,paper_id,search_rank,metadata_json) VALUES(?,?,?,?) ON CONFLICT(run_id,paper_id) DO UPDATE SET search_rank=excluded.search_rank,metadata_json=excluded.metadata_json",
                [
                    (
                        fetch_state.run_id,
                        fetch_state.paper["id"],
                        int(fetch_state.paper.get("search_rank") or fetch_state.index),
                        _services().json.dumps(
                            {
                                fetch_state.key: fetch_state.paper[fetch_state.key]
                                for fetch_state.key in fetch_state.result_metadata_fields
                                if fetch_state.key in fetch_state.paper
                            },
                            ensure_ascii=False,
                        ),
                    )
                    for fetch_state.index, fetch_state.paper in enumerate(fetch_state.results, start=1)
                ],
            )
    # The result snapshot and candidate rows are now complete. Only publish
    # the terminal run state after those writes so every successful poll sees
    # a self-consistent result set.
    fetch_state.completed_at = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as fetch_state.db:
        fetch_state.db.execute(
            "UPDATE runs SET status=?,completed_at=?,new_count=?,duplicate_count=?,source_details_json=?,error=? WHERE id=?",
            (
                fetch_state.status,
                fetch_state.completed_at,
                fetch_state.new_count,
                fetch_state.duplicate_count,
                _services().json.dumps(fetch_state.details, ensure_ascii=False),
                fetch_state.error_text,
                fetch_state.run_id,
            ),
        )
        # A partial provider response is retryable, but waiting an hour makes
        # recovery feel like a lost search. Public-source cooldowns are short
        # and already gate the next request, so expose a bounded five-minute
        # retry hint while preserving the provider's own backoff checks.
        fetch_state.retry_at = (
            (
                _services().datetime.now(_services().timezone.utc)
                + _services().timedelta(minutes=5)
            )
            .replace(microsecond=0)
            .isoformat()
            if fetch_state.errors
            else ""
        )
        fetch_state.db.execute(
            "UPDATE sync_jobs SET status=?,stage=?,last_error=?,next_retry_at=?,updated_at=? WHERE run_id=?",
            (
                fetch_state.status,
                "completed" if fetch_state.status == "success" else "retryable",
                fetch_state.error_text[:1000],
                fetch_state.retry_at,
                fetch_state.completed_at,
                fetch_state.run_id,
            ),
        )
        _services().record_audit(
            fetch_state.db,
            "sync.completed",
            "run",
            fetch_state.run_id,
            {
                "status": fetch_state.status,
                "new_count": fetch_state.new_count,
                "duplicate_count": fetch_state.duplicate_count,
                "error": bool(fetch_state.error_text),
            },
        )
        if fetch_state.query_id:
            # A partial/failed source run is retried soon; a successful run
            # follows the user's normal cadence.
            fetch_state.delay_hours = (
                1 if fetch_state.errors else max(1, int(_services().get_query_cadence(fetch_state.query_id)))
            )
            fetch_state.db.execute(
                "UPDATE saved_queries SET last_run_at=?,next_run_at=?,updated_at=? WHERE id=?",
                (
                    fetch_state.completed_at,
                    (
                        _services().datetime.now(_services().timezone.utc)
                        + _services().timedelta(hours=fetch_state.delay_hours)
                    )
                    .replace(microsecond=0)
                    .isoformat(),
                    fetch_state.completed_at,
                    fetch_state.query_id,
                ),
            )
    fetch_state.response = {
        "id": fetch_state.run_id,
        "status": fetch_state.status,
        "new_count": fetch_state.new_count,
        "duplicate_count": fetch_state.duplicate_count,
        "suppressed_count": fetch_state.suppressed_count,
        "sources": fetch_state.details,
        "error": fetch_state.error_text,
        "papers": fetch_state.results,
        "plan": fetch_state.plan,
        "summary": fetch_state.discovery_summary,
        "candidate_mode": not fetch_state.persist_results,
        "intent_key": fetch_state.intent_key,
        "accepted_count": len(fetch_state.results),
        "requested_count": fetch_state.visible_limit,
        "shortfall_count": fetch_state.shortfall_count,
        "total_fetched": sum(fetch_state.detail.get("fetched", 0) for fetch_state.detail in fetch_state.details),
        "total_filtered": sum(fetch_state.detail.get("filtered", 0) for fetch_state.detail in fetch_state.details),
        "repeat_fallback": fetch_state.repeat_fallback,
        "repeat_fill_count": fetch_state.repeat_fill_count,
        "fresh_candidate_count": fetch_state.fresh_candidate_count,
        "historical_candidate_count": fetch_state.historical_candidate_count,
        "model_web_warning": fetch_state.model_web_warning,
        "model_web_local_fill": fetch_state.discovery_summary.get("model_web_local_fill", 0),
    }
    if not fetch_state.persist_results and fetch_state.results and fetch_state.search_options.get("ai_screen"):
        fetch_state.response["triage"] = _services().start_discovery_ai_screen(fetch_state.run_id)
        fetch_state.response["ai_screening"] = fetch_state.response["triage"]
    elif not fetch_state.persist_results:
        fetch_state.response["triage"] = _services().discovery_triage_state(fetch_state.run_id)

