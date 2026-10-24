"""Fill model search selections from verified and local candidate pools."""

from __future__ import annotations

from ..services import app_services as _services

def _model_search_backfill_selection(
    selected: list[dict],
    inventory: dict,
    selected_ids: set[str],
    query: str,
    target_limit: int,
    options: dict,
) -> tuple[list[dict], int]:
    """Fill a short model selection from candidates the tools already returned.

    The model is asked to rank results, but compatible endpoints often return
    only the few papers they consider strongest.  That is a poor user
    experience when the requested page is larger, and it is especially
    confusing on a repeated topic: the model may omit both historical and
    newly found candidates after trying to avoid repeats.  We can safely fill
    the page from the *real tool inventory* because no metadata is invented;
    the local score only decides order and every filled row is marked as
    uncertain for review.  Strict fresh-only mode still excludes historical
    candidates.
    """
    if len(selected) >= target_limit or not inventory:
        return selected, 0
    facets = _services().extract_search_facets(query)
    has_object_task = bool(facets.get("对象") and facets.get("任务"))
    suppress_seen = bool(options.get("suppress_seen"))
    # The browser enables ``fill_repeats`` together with its normal
    # "prefer unseen" setting.  In that mode historical candidates are a
    # transparent fallback for missing slots; only an explicit strict fresh
    # search should remove them from the local model-tool refill pool.
    strict_fresh_only = suppress_seen and not bool(options.get("fill_repeats"))
    pool = []
    for candidate_id, candidate in inventory.items():
        candidate_id = _services().clean_text(candidate_id, 40)
        if not candidate_id or candidate_id in selected_ids:
            continue
        previously_seen = bool(candidate.get("_previously_seen"))
        if strict_fresh_only and previously_seen:
            continue
        # For structured natural-language questions, do not refill with a
        # method-only paper simply because the model returned it from a broad
        # source.  The normal model-web quality gate performs a second,
        # explainable check later; this first check keeps the queue useful.
        if has_object_task and not _services()._model_web_has_object_task_evidence(
            candidate, facets
        ):
            continue
        triage = _services().score_relevance(candidate, query, [], [])
        score = int(triage.get("score") or 0)
        # Generic keyword searches may have weak lexical overlap even when a
        # provider's semantic index found a useful item.  Natural-language
        # questions need a minimal signal before an automatic refill.
        if has_object_task and score < 12:
            continue
        pool.append(
            (
                previously_seen,
                score,
                int(candidate.get("citation_count") or 0),
                candidate_id,
                triage,
            )
        )
    pool.sort(key=lambda item: (item[0], -item[1], -item[2], item[3]))
    added = 0
    for previously_seen, score, _citations, candidate_id, triage in pool:
        if len(selected) >= target_limit:
            break
        candidate = inventory[candidate_id]
        selected_ids.add(candidate_id)
        label = "adjacent" if score >= 35 else "uncertain"
        origin = "历史候选" if previously_seen else "新候选"
        selected.append(
            {
                **candidate,
                "relevance_label": label,
                "relevance_reason": (
                    f"模型检索工具已返回；模型主筛选未列入，按本地相关性排序补位（{origin}，"
                    f"相关性分数 {score}），建议人工复核。"
                ),
                "source_citations": candidate.get("source_citations")
                or [candidate.get("url", "")],
                "_selection_backfilled": True,
            }
        )
        added += 1
    return selected, added


def _model_search_local_fill(
    selected: list[dict],
    inventory: dict,
    identity_index: dict,
    selected_ids: set[str],
    query: str,
    target_limit: int,
    options: dict,
    preferred_sources: list[str],
    seen_titles: set[str],
    seen_identities: set[str],
    executed_searches: list[dict],
) -> tuple[list[dict], int, int]:
    """Use bounded local connector calls to fill a short model result page.

    A model is useful for choosing search angles and ranking candidates, but a
    compatible endpoint may return only one or two IDs even after the tools
    found more work.  When that happens, run at most two *new* deterministic
    query variants against the user's selected academic connectors.  The
    connector records are real and are passed through the same identity,
    relevance and repeat logic; candidates not explicitly ranked by the model
    are marked as local-fill/uncertain by ``_model_search_backfill_selection``.
    This keeps result counts stable without inventing metadata or spending
    another model request.
    """
    if len(selected) >= target_limit:
        return selected, 0, 0
    try:
        safe_target = min(40, max(1, int(target_limit)))
    except (TypeError, ValueError):
        safe_target = 20
    # The model-web caller normally passes the user's selected connector set.
    # When it is empty, the model's tool calls below provide the authoritative
    # source list; that observed list is used by the bounded refill below.
    source_pool = [
        source
        for source in preferred_sources
        if source in _services().SOURCE_SEARCH and source != "model_web"
    ]
    # In AI-tool mode the user is allowed to leave the source list empty: the
    # model chooses academic connectors in its function calls.  The old local
    # refill treated that as "no sources" and returned immediately, so a
    # short model ranking (or one quality-gate rejection) could make the
    # visible page smaller solely because repeat avoidance was enabled.  Reuse
    # the actual sources observed in this run before falling back to the
    # explicitly preferred list.  Keep academic indexes first and Tavily last;
    # Tavily is useful for recall but its unverified rows should not displace
    # a source-backed paper during a bounded refill.
    if not source_pool:
        observed_sources = []
        for search in executed_searches:
            for summary in (
                search.get("sources", []) if isinstance(search, dict) else []
            ):
                source = summary.get("source") if isinstance(summary, dict) else ""
                if (
                    source in _services().SOURCE_SEARCH
                    and source != "model_web"
                    and source not in observed_sources
                ):
                    observed_sources.append(source)
        source_pool = [source for source in observed_sources if source != "tavily"]
        source_pool.extend(source for source in observed_sources if source == "tavily")
    source_pool = list(dict.fromkeys(source_pool))[
        : _services().MODEL_SEARCH_TOOL_MAX_SOURCES
    ]
    if not source_pool:
        return selected, 0, 0
    try:
        local_plan = _services().search_query_plan(
            query, source_pool, {**options, "adaptive_expansion": False}
        )
    except Exception:
        local_plan = {}
    variants = local_plan.get("query_variants") if isinstance(local_plan, dict) else []
    if not isinstance(variants, list):
        variants = []
    already_searched = {
        _services().clean_text(item.get("query"), 500).casefold()
        for item in executed_searches
        if isinstance(item, dict) and _services().clean_text(item.get("query"), 500)
    }
    local_queries = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        variant_query = _services().clean_text(variant.get("query"), 500)
        if not variant_query or variant_query.casefold() in already_searched:
            continue
        local_queries.append(variant_query)
        if len(local_queries) >= 2:
            break
    if not local_queries:
        fallback_query = _services().clean_text(query, 500)
        if not fallback_query or fallback_query.casefold() in already_searched:
            return selected, 0, 0
        local_queries = [fallback_query]

    arguments = [
        {
            "query": variant_query,
            "sources": source_pool,
            "limit": min(_services().MODEL_SEARCH_TOOL_MAX_LIMIT, max(6, safe_target)),
        }
        for variant_query in local_queries
    ]
    with _services().ThreadPoolExecutor(
        max_workers=min(2, len(arguments)), thread_name_prefix="model-search-local-fill"
    ) as pool:
        prefetched_batches = list(
            pool.map(
                lambda item: _services()._model_search_prefetch(
                    item,
                    source_pool,
                    seen_titles,
                    seen_identities,
                    options.get("avoid_repeats", True),
                ),
                arguments,
            )
        )

    for argument, prefetched in zip(arguments, prefetched_batches):
        if not prefetched:
            continue
        output = _services()._model_search_tool_result(
            argument,
            inventory,
            identity_index,
            seen_titles,
            seen_identities,
            options,
            source_pool,
            prefetched,
        )
        executed_searches.append(
            {
                "query": output.get("query") or argument["query"],
                "sources": output.get("source_results", []),
                "result_count": len(output.get("results", [])),
                "excluded_previously_seen": output.get("excluded_previously_seen", 0),
                "local_fill": True,
            }
        )
    before = len(selected)
    selected, added = _services()._model_search_backfill_selection(
        selected,
        inventory,
        selected_ids,
        query,
        safe_target,
        options,
    )
    return selected, added, max(0, len(selected) - before)


__all__ = ['_model_search_backfill_selection', '_model_search_local_fill']
