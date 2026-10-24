"""Search run ranking and state transitions."""

from __future__ import annotations

from ..services import app_services as _services

def _rank_search_candidates(fetch_state) -> None:

    # A source/model failure can still leave a complete, reviewable page from
    # the durable history fallback. Keep the diagnostic error, but publish the
    # run as partial so the UI does not discard valid candidates as a failed
    # search. Runs with no results remain failed and retryable.
    if fetch_state.status == "failed" and fetch_state.results:
        fetch_state.status = "partial"

    # Candidate/review-only runs use durable ``candidate:<uuid>`` records
    # until the user explicitly imports them.  ``query_matches.paper_id``
    # references the real ``papers`` table, so writing candidate IDs here
    # violates the foreign key and can turn a successful search into a
    # misleading failed run.  Query memory is only materialized after the
    # papers have been persisted (or for an explicitly persisted run).
    if fetch_state.query_id and fetch_state.results and fetch_state.persist_results:
        # Query-specific memory prevents an ignore decision in one topic from
        # hiding the same work from another topic.
        fetch_state.observed_at = _services().now_iso()
        with _services().DB_LOCK, _services().db_session() as fetch_state.db:
            for fetch_state.paper in fetch_state.results:
                fetch_state.existing = fetch_state.db.execute(
                    "SELECT first_seen_at,seen_count,user_decision,viewed_at FROM query_matches WHERE query_id=? AND paper_id=?",
                    (fetch_state.query_id, fetch_state.paper["id"]),
                ).fetchone()
                if fetch_state.existing:
                    fetch_state.db.execute(
                        """UPDATE query_matches SET last_seen_at=?,seen_count=?,last_run_id=?,score=?,tier=?,explanation_json=?,score_version=?
                                  WHERE query_id=? AND paper_id=?""",
                        (
                            fetch_state.observed_at,
                            fetch_state.existing["seen_count"] + 1,
                            fetch_state.run_id,
                            fetch_state.paper.get("discovery_score", fetch_state.paper["relevance_score"]),
                            fetch_state.paper.get("discovery_class", fetch_state.paper["relevance_label"]),
                            _services().json.dumps(
                                fetch_state.paper.get(
                                    "discovery_reasons", fetch_state.paper["relevance_reasons"]
                                ),
                                ensure_ascii=False,
                            ),
                            "rankfusion-v2",
                            fetch_state.query_id,
                            fetch_state.paper["id"],
                        ),
                    )
                    fetch_state.paper["seen_count"] = fetch_state.existing["seen_count"] + 1
                    fetch_state.paper["first_seen_at"] = fetch_state.existing["first_seen_at"]
                    fetch_state.paper["user_decision"] = fetch_state.existing["user_decision"]
                    fetch_state.paper["viewed_at"] = fetch_state.existing["viewed_at"]
                    fetch_state.paper["suppressed"] = fetch_state.existing["user_decision"] in {
                        "irrelevant",
                        "ignored",
                    }
                else:
                    fetch_state.db.execute(
                        """INSERT INTO query_matches(query_id,paper_id,first_seen_at,last_seen_at,seen_count,first_run_id,last_run_id,score,tier,explanation_json,score_version,user_decision,viewed_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            fetch_state.query_id,
                            fetch_state.paper["id"],
                            fetch_state.observed_at,
                            fetch_state.observed_at,
                            1,
                            fetch_state.run_id,
                            fetch_state.run_id,
                            fetch_state.paper.get("discovery_score", fetch_state.paper["relevance_score"]),
                            fetch_state.paper.get("discovery_class", fetch_state.paper["relevance_label"]),
                            _services().json.dumps(
                                fetch_state.paper.get(
                                    "discovery_reasons", fetch_state.paper["relevance_reasons"]
                                ),
                                ensure_ascii=False,
                            ),
                            "rankfusion-v2",
                            "new",
                            "",
                        ),
                    )
                    fetch_state.paper["seen_count"] = 1
                    fetch_state.paper["first_seen_at"] = fetch_state.observed_at
                    fetch_state.paper["user_decision"] = "new"
                    fetch_state.paper["viewed_at"] = ""
                    fetch_state.paper["suppressed"] = False
    fetch_state.suppressed_count = sum(1 for fetch_state.paper in fetch_state.results if fetch_state.paper.get("suppressed"))
    fetch_state.before_suppress_count = len(fetch_state.results)
    fetch_state.repeat_fallback = False
    fetch_state.repeat_fallback_for_ai_screen = False
    fetch_state.repeat_fill_count = 0
    fetch_state.fresh_candidate_count = 0
    fetch_state.historical_candidate_count = 0
    # ``avoid_repeats`` is a ranking preference in the normal review queue.
    # It must never turn a requested page of N results into a page of two just
    # because the other candidates were seen in an earlier run.  Keep the
    # fresh candidates first, then use historical candidates as a transparent
    # fill.  ``suppress_seen`` below remains the explicit strict mode.
    if fetch_state.search_options.get("avoid_repeats", True) and not fetch_state.search_options.get(
        "suppress_seen"
    ):
        fetch_state.fresh = [
            fetch_state.paper
            for fetch_state.paper in fetch_state.results
            if not fetch_state.paper.get("seen_before") or fetch_state.paper.get("user_decision") == "relevant"
        ]
        fetch_state.historical = [fetch_state.paper for fetch_state.paper in fetch_state.results if fetch_state.paper not in fetch_state.fresh]
        fetch_state.fresh_candidate_count = len(fetch_state.fresh)
        fetch_state.historical_candidate_count = len(fetch_state.historical)
        fetch_state.results = [*fetch_state.fresh, *fetch_state.historical]
        try:
            fetch_state.requested_page_size = min(100, max(1, int(fetch_state.limit)))
        except (TypeError, ValueError):
            fetch_state.requested_page_size = 25
        fetch_state.repeat_fill_count = min(
            len(fetch_state.historical), max(0, fetch_state.requested_page_size - len(fetch_state.fresh))
        )
        if fetch_state.repeat_fill_count:
            fetch_state.repeat_fallback = True
        elif not fetch_state.fresh and fetch_state.historical:
            fetch_state.repeat_fallback = True
    if fetch_state.search_options.get("suppress_seen"):
        fetch_state.unseen = [
            fetch_state.paper
            for fetch_state.paper in fetch_state.results
            if not fetch_state.paper.get("seen_before") or fetch_state.paper.get("user_decision") == "relevant"
        ]
        fetch_state.historical = [
            fetch_state.paper
            for fetch_state.paper in fetch_state.results
            if fetch_state.paper not in fetch_state.unseen
            and fetch_state.paper.get("user_decision") not in {"irrelevant", "ignored"}
        ]
        # The web application uses ``fill_repeats`` so the fresh-only
        # preference cannot collapse a requested page after a source returns
        # mostly historical rows.  Keep fresh rows first and fill only the
        # missing slots from candidates the user has not rejected.  The
        # explicit API mode without this flag remains strict for compatibility.
        if fetch_state.search_options.get("fill_repeats"):
            try:
                fetch_state.requested_page_size = min(100, max(1, int(fetch_state.limit)))
            except (TypeError, ValueError):
                fetch_state.requested_page_size = 25
            fetch_state.needed = max(0, fetch_state.requested_page_size - len(fetch_state.unseen))
            fetch_state.fallback = fetch_state.historical[:fetch_state.needed] if fetch_state.needed else []
            fetch_state.results = [*fetch_state.unseen, *fetch_state.fallback]
            if fetch_state.fallback:
                fetch_state.repeat_fallback = True
                fetch_state.repeat_fill_count = len(fetch_state.fallback)
                for fetch_state.paper in fetch_state.fallback:
                    fetch_state.reasons = fetch_state.paper.setdefault("discovery_reasons", [])
                    fetch_state.message = "本轮优先展示新论文；为保持请求数量，补充此前出现的候选，建议人工复核"
                    if fetch_state.message not in fetch_state.reasons:
                        fetch_state.reasons.append(fetch_state.message)
        elif fetch_state.unseen:
            fetch_state.results = fetch_state.unseen
        elif fetch_state.results:
            # Strict mode intentionally has no fresh candidates. Keep this
            # state explicit so AI screening can offer a reviewable fallback.
            fetch_state.repeat_fallback = True
            if fetch_state.search_options.get("ai_screen"):
                # An AI review queue cannot do useful work when strict
                # filtering turns a fully repeat query into an empty snapshot.
                # Keep only candidates that the user has not explicitly
                # rejected, and make the reason visible so a reviewer can
                # dismiss or import them.
                fetch_state.repeat_candidates = [
                    fetch_state.paper
                    for fetch_state.paper in fetch_state.results
                    if fetch_state.paper.get("user_decision") not in {"irrelevant", "ignored"}
                ]
                if fetch_state.repeat_candidates:
                    fetch_state.results = fetch_state.repeat_candidates
                    fetch_state.repeat_fallback_for_ai_screen = True
                    for fetch_state.paper in fetch_state.results:
                        fetch_state.reasons = fetch_state.paper.setdefault("discovery_reasons", [])
                        fetch_state.message = (
                            "本轮没有新的未见论文，保留历史相关候选供 AI 初筛和人工复核"
                        )
                        if fetch_state.message not in fetch_state.reasons:
                            fetch_state.reasons.append(fetch_state.message)
                else:
                    fetch_state.results = []
            else:
                fetch_state.results = []
        else:
            fetch_state.results = []
    if fetch_state.search_options.get("suppress_seen"):
        fetch_state.fresh_candidate_count = sum(
            1
            for fetch_state.paper in fetch_state.results
            if not fetch_state.paper.get("seen_before") or fetch_state.paper.get("user_decision") == "relevant"
        )
        fetch_state.historical_candidate_count = max(
            0, fetch_state.before_suppress_count - fetch_state.fresh_candidate_count
        )
    fetch_state.suppressed_count = max(fetch_state.suppressed_count, fetch_state.before_suppress_count - len(fetch_state.results))
    fetch_state.sort_mode = fetch_state.search_options.get("sort", "relevance")
    fetch_state.discovery_mode = fetch_state.search_options.get("discovery_mode", "balanced")
    if fetch_state.sort_mode == "newest":
        fetch_state.results.sort(
            key=lambda item: (item.get("year") or 0, item.get("relevance_score") or 0),
            reverse=True,
        )
    elif fetch_state.sort_mode == "oldest":
        fetch_state.results.sort(
            key=lambda item: (
                item.get("year") or 9999,
                -(item.get("relevance_score") or 0),
            )
        )
    elif fetch_state.sort_mode == "novelty":
        fetch_state.results.sort(
            key=lambda item: (
                item.get("novelty_score") or 0,
                item.get("relevance_score") or 0,
                item.get("year") or 0,
            ),
            reverse=True,
        )
    elif fetch_state.sort_mode == "foundations" or fetch_state.discovery_mode == "foundations":
        fetch_state.results.sort(
            key=lambda item: (
                item.get("impact_score") or 0,
                item.get("relevance_score") or 0,
                item.get("year") or 0,
            ),
            reverse=True,
        )
    elif fetch_state.discovery_mode == "latest":
        fetch_state.results.sort(
            key=lambda item: (
                item.get("recency_score") or 0,
                item.get("relevance_score") or 0,
                item.get("source_quality_score") or 0,
            ),
            reverse=True,
        )
    elif fetch_state.discovery_mode == "explore":
        fetch_state.results.sort(
            key=lambda item: (
                item.get("relevance_score") or 0,
                item.get("source_quality_score") or 0,
                item.get("novelty_score") or 0,
            ),
            reverse=True,
        )
    else:
        fetch_state.results.sort(
            key=lambda item: (
                item.get("discovery_score") or 0,
                item.get("relevance_score") or 0,
                item.get("year") or 0,
            ),
            reverse=True,
        )
    if fetch_state.search_options.get("diversity", True):
        fetch_state.results = _services().diversify_results(fetch_state.results)
    # ``limit`` is the user-facing review queue size, not only a connector
    # fetch hint. Enforce it after relevance/novelty sorting so a broad
    # natural-language expansion cannot flood the page or the AI screener.
    try:
        fetch_state.visible_limit = min(100, max(1, int(fetch_state.limit)))
    except (TypeError, ValueError):
        fetch_state.visible_limit = 25
    if fetch_state.search_options.get("avoid_repeats", True) and not fetch_state.search_options.get(
        "suppress_seen"
    ):
        # Re-apply the novelty-first partition after ranking/diversity.  The
        # ranking modes may otherwise interleave old and new records and make
        # the user's "优先展示本课题新论文" setting look ineffective.
        fetch_state.fresh = [
            fetch_state.paper
            for fetch_state.paper in fetch_state.results
            if not fetch_state.paper.get("seen_before") or fetch_state.paper.get("user_decision") == "relevant"
        ]
        fetch_state.historical = [fetch_state.paper for fetch_state.paper in fetch_state.results if fetch_state.paper not in fetch_state.fresh]
        fetch_state.fresh_candidate_count = len(fetch_state.fresh)
        fetch_state.historical_candidate_count = len(fetch_state.historical)
        fetch_state.results = [*fetch_state.fresh, *fetch_state.historical]
        fetch_state.repeat_fill_count = min(len(fetch_state.historical), max(0, fetch_state.visible_limit - len(fetch_state.fresh)))
        fetch_state.repeat_fallback = bool(fetch_state.repeat_fill_count or (not fetch_state.fresh and fetch_state.historical))
    fetch_state.truncated_count = max(0, len(fetch_state.results) - fetch_state.visible_limit)
    if fetch_state.truncated_count:
        fetch_state.results = fetch_state.results[:fetch_state.visible_limit]
    fetch_state.shortfall_count = max(0, fetch_state.visible_limit - len(fetch_state.results))
    for fetch_state.index, fetch_state.paper in enumerate(fetch_state.results, start=1):
        fetch_state.paper["search_rank"] = fetch_state.index
    fetch_state.discovery_summary = {
        "high_relevance": sum(
            1 for fetch_state.paper in fetch_state.results if fetch_state.paper.get("relevance_label") == "高相关"
        ),
        "adjacent": sum(
            1 for fetch_state.paper in fetch_state.results if fetch_state.paper.get("relevance_label") == "邻近方向"
        ),
        "new_to_topic": sum(1 for fetch_state.paper in fetch_state.results if not fetch_state.paper.get("seen_before")),
        "open_access": sum(1 for fetch_state.paper in fetch_state.results if fetch_state.paper.get("open_access")),
        "with_citations": sum(1 for fetch_state.paper in fetch_state.results if fetch_state.paper.get("citation_count")),
        "lanes": sorted(
            {fetch_state.lane for fetch_state.paper in fetch_state.results for fetch_state.lane in fetch_state.paper.get("discovery_lanes", [])}
        ),
        "repeat_count": sum(1 for fetch_state.paper in fetch_state.results if fetch_state.paper.get("seen_before")),
        "repeat_fallback": fetch_state.repeat_fallback,
        "repeat_fallback_for_ai_screen": fetch_state.repeat_fallback_for_ai_screen,
        "repeat_fill_count": fetch_state.repeat_fill_count,
        "fresh_candidate_count": fetch_state.fresh_candidate_count,
        "historical_candidate_count": fetch_state.historical_candidate_count,
        "result_limit": fetch_state.visible_limit,
        "result_count": len(fetch_state.results),
        "shortfall_count": fetch_state.shortfall_count,
        "shortfall_reason": (
            "只看未重复模式主动隐藏历史候选，当前来源没有足够的新论文"
            if fetch_state.shortfall_count
            and fetch_state.search_options.get("suppress_seen")
            and not fetch_state.search_options.get("fill_repeats")
            else (
                "来源、条件过滤或相关性门槛后可核验候选不足；未用无关论文凑数"
                if fetch_state.shortfall_count
                else ""
            )
        ),
        "repeat_policy": (
            "strict_fresh_only"
            if fetch_state.search_options.get("suppress_seen")
            and not fetch_state.search_options.get("fill_repeats")
            else (
                "new_first_history_fill"
                if fetch_state.search_options.get("avoid_repeats", True)
                else "no_repeat_preference"
            )
        ),
        "truncated_count": fetch_state.truncated_count,
        "search_backend": fetch_state.search_options.get("search_backend", "connectors"),
        "search_backend_executed": fetch_state.plan.get(
            "search_backend_executed",
            fetch_state.search_options.get("search_backend", "connectors"),
        ),
        "model_web_fallback": fetch_state.model_web_fallback,
        "model_web_warning": fetch_state.model_web_warning,
        "model_web_quality_filtered": fetch_state.model_web_quality_filtered,
        "model_web_quality_refill": fetch_state.model_web_quality_refill,
        "model_web_backfilled": (
            int(fetch_state.model_web_result.get("backfill_count") or 0)
            if isinstance(fetch_state.model_web_result, dict)
            else 0
        ),
        "model_web_local_fill": (
            int(fetch_state.model_web_result.get("local_fill_count") or 0)
            if isinstance(fetch_state.model_web_result, dict)
            else 0
        ),
        "model_web_gaps": (
            fetch_state.model_web_result.get("gaps", [])
            if isinstance(fetch_state.model_web_result, dict)
            else []
        ),
        "model_web_next_queries": (
            fetch_state.model_web_result.get("next_queries", [])
            if isinstance(fetch_state.model_web_result, dict)
            else []
        ),
        "model_search_timing": (
            fetch_state.plan.get("model_search_timing", {}) if isinstance(fetch_state.plan, dict) else {}
        ),
        "model_web_source_errors": fetch_state.model_web_source_errors[:12],
    }

