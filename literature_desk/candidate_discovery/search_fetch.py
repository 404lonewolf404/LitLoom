"""Execute planned search lanes and stage provider results."""

from __future__ import annotations

from types import SimpleNamespace

from ..services import app_services as _services
from .search_setup import _prepare_search_run

def _fetch_search_sources(
    query: str,
    sources: list[str],
    limit: int,
    query_id: str,
    scheduled: bool,
    name: str,
    options: dict | None,
    request_id: str,
    plan_override: dict | None,
) -> SimpleNamespace:
    setup = _prepare_search_run(query, sources, limit, query_id, scheduled, name, options, request_id, plan_override)
    details, new_count, duplicate_count = [], 0, 0
    paper_ids = []
    paper_paths = {}
    paper_signals = {}
    paper_rank_signals = {}
    result_created = {}
    candidate_records = {}
    # Some fallback candidates are discovered after the initial candidate
    # staging pass (for example, history refill after the model quality gate).
    # Keep them separate so they can be persisted into this run before its
    # snapshot is published; otherwise a refresh can show a count without
    # the corresponding candidate rows.
    late_candidate_records = {}
    candidate_identity_to_id = {}
    search_observations = {}
    errors = []
    # A saved topic is intentionally allowed to spend more network budget.
    # One-off searches and Agent candidate scans must stay responsive even
    # when a connector is slow or the query has many expansion variants.
    persistent_search = bool(query_id or scheduled)
    search_depth = setup.search_options.get("search_depth", "balanced")
    source_prefetch = {}
    fetch_sources = list(setup.sources)
    # A repeat search may have a short connector page even after continuation
    # fetches: several providers can return the same work under different
    # identifiers, while the requested page is measured in unique papers.
    # Give the first successful connector one durable-history refill chance so
    # duplicate handling never silently turns a requested page into a handful
    # of rows.  Model-web searches have their own tool-aware refill path.
    history_fill_attempted = False
    model_web_warning = ""
    model_web_fallback = False
    model_web_result = {}
    model_web_source_errors = []
    if setup.search_options.get("search_backend") == "model_web":
        # The model orchestrates academic connectors through standard function
        # calls. A deterministic connector fallback remains explicit.
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "UPDATE sync_jobs SET stage=?,updated_at=? WHERE id=?",
                ("model_web_search", _services().now_iso(), setup.run_id),
            )
            seen_identities = {
                _services().clean_text(row[0], 700)
                for row in db.execute(
                    "SELECT identity_key FROM discovery_search_memory WHERE intent_key=?",
                    (setup.intent_key,),
                )
                if _services().clean_text(row[0], 700)
            }
            seen_titles = [
                _services().clean_text(row[0], 300)
                for row in db.execute(
                    "SELECT title,MAX(last_seen_at) AS last_seen FROM discovery_search_memory WHERE intent_key=? AND title<>'' GROUP BY title ORDER BY last_seen DESC LIMIT 100",
                    (setup.intent_key,),
                )
                if _services().clean_text(row[0], 300)
            ]

        def update_model_search_progress(message: str) -> None:
            with _services().DB_LOCK, _services().db_session() as db:
                db.execute(
                    "UPDATE sync_jobs SET stage=?,updated_at=? WHERE id=?",
                    (
                        "model_web_search:" + _services().clean_text(message, 120),
                        _services().now_iso(),
                        setup.run_id,
                    ),
                )

        model_search_options = {
            **setup.search_options,
            "_seen_titles": seen_titles,
            "_seen_identities": list(seen_identities),
            "_preferred_sources": setup.sources,
            "_progress_callback": update_model_search_progress,
        }
        setup.plan["model_search_memory"] = {
            "excluded_title_count": len(seen_titles),
            "excluded_identity_count": len(seen_identities),
        }
        try:
            web_result = _services().model_web_search(setup.q, limit, model_search_options)
            model_web_result = web_result
            for search in web_result.get("searches") or []:
                if not isinstance(search, dict):
                    continue
                for source_summary in search.get("sources") or []:
                    if not isinstance(source_summary, dict) or not source_summary.get(
                        "error"
                    ):
                        continue
                    source_name = _services().MODEL_SEARCH_CONNECTOR_LABELS.get(
                        source_summary.get("source"),
                        source_summary.get("source", "来源"),
                    )
                    message = f"{source_name}：{_services().clean_text(source_summary.get('error'), 300)}"
                    if message not in model_web_source_errors:
                        model_web_source_errors.append(message)
            model_web_source_errors = model_web_source_errors[:12]
            web_records = web_result.get("results") or []
            # Persist timing diagnostics with the run.  Search progress is
            # durable, but without these measurements a slow relay and a
            # slow public connector look identical in the UI after refresh.
            timing = (
                web_result.get("timing")
                if isinstance(web_result.get("timing"), dict)
                else {}
            )
            setup.plan["model_search_timing"] = {
                "duration_ms": float(timing.get("duration_ms") or 0),
                "model_request_total_ms": float(
                    timing.get("model_request_total_ms") or 0
                ),
                "tool_call_total_ms": float(timing.get("tool_call_total_ms") or 0),
                "model_request_count": len(
                    timing.get("model_request_durations_ms") or []
                ),
                "tool_call_count": len(timing.get("tool_call_durations_ms") or []),
                "model_request_durations_ms": [
                    float(value or 0)
                    for value in (timing.get("model_request_durations_ms") or [])[:20]
                ],
                "tool_call_durations_ms": [
                    float(value or 0)
                    for value in (timing.get("tool_call_durations_ms") or [])[:40]
                ],
                "repair_count": int(web_result.get("repair_count") or 0),
                "retry_count": int(web_result.get("retry_count") or 0),
            }
            # A repeated model-led search can legitimately discover fewer
            # fresh rows than requested even after several tool angles.  Keep
            # the visible page useful by reusing durable candidates from the
            # same research intent.  This is a transparent history lane: it
            # is still quality-gated below, never auto-imported, and never
            # used when the user explicitly selected "只看未重复".
            # On a repeated topic the model's final list can already contain
            # ``limit`` rows, but the local object/task gate may remove some
            # of them afterwards.  Reserve a bounded history buffer before
            # that gate so accepted historical candidates can replace only
            # the rows that were actually rejected.  New topics keep the
            # original fast path and do not scan the durable queue.
            try:
                requested_limit = min(100, max(1, int(limit)))
            except (TypeError, ValueError):
                requested_limit = 25
            history_target = min(100, max(requested_limit, requested_limit * 2))
            if (
                not setup.search_options.get("suppress_seen")
                or setup.search_options.get("fill_repeats")
            ) and (len(web_records) < requested_limit or setup.prior_memory_count > 0):
                history_fill = _services()._model_search_history_fill(
                    setup.intent_key,
                    setup.q,
                    web_records,
                    history_target,
                    setup.search_options,
                )
                if history_fill:
                    for item in history_fill:
                        item["selection_backfilled"] = True
                        item.setdefault("relevance_label", "uncertain")
                        item.setdefault(
                            "relevance_reason",
                            "同一研究问题的历史候选补位；模型本轮未重新排序，建议人工复核",
                        )
                    web_records = [*web_records, *history_fill]
                    model_web_result["history_fill_count"] = len(history_fill)
            if web_records:
                setup.plan["search_backend_requested"] = "model_web"
                setup.plan["search_backend_executed"] = "model_web"
                tool_searches = web_result.get("searches") or []
                variants, variant_index = [], {}
                for search_index, search in enumerate(tool_searches, start=1):
                    search_query = (
                        _services().clean_text(search.get("query"), 500)
                        if isinstance(search, dict)
                        else ""
                    )
                    for source_result in (
                        search.get("sources", []) if isinstance(search, dict) else []
                    ):
                        connector = (
                            source_result.get("source")
                            if isinstance(source_result, dict)
                            else ""
                        )
                        if connector not in _services().SOURCE_SEARCH:
                            continue
                        key = (connector, search_query)
                        if key in variant_index:
                            continue
                        variant = {
                            "id": f"model-tool-{len(variants) + 1}",
                            "query": search_query or setup.q,
                            "kind": _services().MODEL_SEARCH_CONNECTOR_LABELS.get(
                                connector, connector
                            ),
                            "source": connector,
                            "reason": "模型根据研究问题选择检索式和学术来源",
                            "lane": "model-tool",
                        }
                        variant_index[key] = len(variants)
                        variants.append(variant)
                if not variants:
                    variants = [
                        {
                            "id": "model-tool-1",
                            "query": setup.q,
                            "kind": "AI 工具检索",
                            "source": "",
                            "reason": "模型规划检索",
                            "lane": "model-tool",
                        }
                    ]
                    variant_index[("", setup.q)] = 0
                batches = [[] for _ in variants]
                for item in web_records:
                    paths = (
                        item.get("search_paths")
                        if isinstance(item.get("search_paths"), list)
                        else []
                    )
                    assigned = False
                    for path in paths:
                        key = (
                            (path.get("source", ""), path.get("query", setup.q))
                            if isinstance(path, dict)
                            else ("", setup.q)
                        )
                        index = variant_index.get(key)
                        if index is not None:
                            batches[index].append(
                                dict(
                                    item,
                                    _search_variant=variants[index]["id"],
                                    _search_query=variants[index]["query"],
                                    _source_rank=len(batches[index]) + 1,
                                    _variant_rank=index + 1,
                                )
                            )
                            assigned = True
                    if not assigned:
                        batches[0].append(
                            dict(
                                item,
                                _search_variant=variants[0]["id"],
                                _search_query=variants[0]["query"],
                                _source_rank=len(batches[0]) + 1,
                                _variant_rank=1,
                            )
                        )
                connector_ids = list(
                    dict.fromkeys(
                        item["source"] for item in variants if item.get("source")
                    )
                )
                setup.plan["strategy"] = (
                    "模型拆解研究问题并循环调用 LitLoom 论文检索工具 → 并行查询匹配的学术来源 → 模型筛选真实来源候选 → 按本课题历史去重"
                )
                setup.plan["query_variants"] = variants
                setup.plan["expansion_count"] = max(0, len(variants) - 1)
                setup.plan["coverage_layers"] = [
                    "AI 规划检索角度",
                    *[
                        _services().MODEL_SEARCH_CONNECTOR_LABELS.get(source, source)
                        for source in connector_ids
                    ],
                ]
                setup.plan["sources"] = [
                    {"id": source, "role": "模型通过检索工具选择的学术来源"}
                    for source in connector_ids
                ]
                setup.plan["model_tool_calls"] = web_result.get("tool_calls", 0)
                setup.plan["model_requests"] = web_result.get("attempts", 0)
                source_prefetch["model_web"] = {
                    "variants": variants,
                    "batches": batches,
                    "model_result": web_result,
                }
                fetch_sources = ["model_web"]
            else:
                details_text = "；".join(web_result.get("gaps") or [])
                model_web_warning = "AI 工具检索已执行，但没有筛选出相关候选" + (
                    f"：{details_text}"
                    if details_text
                    else "。可调整研究问题或放宽筛选条件。"
                )
        except _services().ModelWebSearchUnsupported as exc:
            model_web_warning = str(exc)
        except (
            _services().HTTPError,
            _services().URLError,
            TimeoutError,
            OSError,
            ValueError,
            _services().json.JSONDecodeError,
        ) as exc:
            model_web_warning = (
                f"AI 工具检索失败：{_services().clean_text(str(exc), 300)}"
            )
        if model_web_warning:
            setup.plan["search_backend_requested"] = "model_web"
            setup.plan["search_backend_executed"] = (
                "connectors_fallback"
                if setup.search_options.get("model_web_fallback_to_connectors")
                else "model_web_failed"
            )
            setup.plan["search_fallback_reason"] = model_web_warning
            if setup.search_options.get("model_web_fallback_to_connectors"):
                model_web_warning += " 已按设置切换到学术数据库连接器。"
                model_web_fallback = True
                details.append(
                    {
                        "source": "model_web",
                        "status": "fallback",
                        "fetched": 0,
                        "accepted": 0,
                        "filtered": 0,
                        "variant_duplicates": 0,
                        "new": 0,
                        "duplicates": 0,
                        "variants": [],
                        "error": model_web_warning,
                        "fallback": True,
                    }
                )
            else:
                source_prefetch["model_web"] = {"error": ValueError(model_web_warning)}
                fetch_sources = ["model_web"]
                setup.plan["sources"] = [
                    {"id": "model_web", "role": "模型通过 LitLoom 学术检索工具搜索"}
                ]
    if setup.search_options.get("parallel_sources") and len(fetch_sources) > 1:
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "UPDATE sync_jobs SET stage=?,updated_at=? WHERE id=?",
                ("fetching_parallel", _services().now_iso(), setup.run_id),
            )
        with _services().ThreadPoolExecutor(
            max_workers=min(4, len(fetch_sources)), thread_name_prefix="search-source"
        ) as pool:
            futures = {
                source: pool.submit(
                    _services()._prefetch_source_variants,
                    source,
                    setup.plan,
                    limit,
                    persistent_search,
                    search_depth,
                    setup.refill_repeats,
                    setup.prior_intent_identities,
                )
                for source in fetch_sources
            }
            for source, future in futures.items():
                try:
                    source_prefetch[source] = future.result()
                except Exception as exc:
                    source_prefetch[source] = {"error": exc}
    for source in fetch_sources:
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "UPDATE sync_jobs SET stage=?,updated_at=? WHERE id=?",
                (f"source:{source}", _services().now_iso(), setup.run_id),
            )
        try:
            page_size = min(max(int(limit), 1), 50)
            fetch_size = 50 if persistent_search else page_size
            # Saved topics fetch a few pages so a burst of new papers cannot
            # hide everything after the first 50 results. Ad-hoc searches stay
            # lighter and fetch one page.
            widen_for_fresh = bool(
                setup.search_options.get("suppress_seen")
                and (setup.search_options.get("ai_expand") or setup.search_options.get("ai_screen"))
            )
            # A second page is a conditional refill: the loop stops after a
            # full unique page, and only continues when provider aliases or
            # cross-index duplicates leave the requested count short.
            # Existing topics and strict fresh-only searches get bounded
            # continuation pages. This is the refill path that keeps
            # historical overlap from reducing a normal requested page to a
            # handful of records while retaining the fast one-page path for
            # new topics.
            max_pages = (
                8
                if setup.refill_repeats
                else (3 if persistent_search or widen_for_fresh else 2)
            )
            try:
                requested_unseen = min(100, max(1, int(limit)))
            except (TypeError, ValueError):
                requested_unseen = 25

            def repeat_unseen_count(records: list[dict]) -> int:
                if not setup.refill_repeats or not setup.prior_intent_identities:
                    return len(_services().merge_search_records(records)[0])
                count = 0
                for record in _services().merge_search_records(records)[0]:
                    identities = _services().candidate_identity_variants(record)
                    if not identities or not identities.intersection(
                        setup.prior_intent_identities
                    ):
                        count += 1
                return count

            all_records = []
            variant_details = []
            variants = _services()._prioritize_ai_search_variants(setup.plan)
            if source == "model_web":
                variants = source_prefetch.get("model_web", {}).get("variants") or [
                    {
                        "id": "model-tool-1",
                        "query": setup.q,
                        "kind": "AI 工具检索",
                        "reason": "模型规划检索",
                        "lane": "model-tool",
                    }
                ]
            if source == "semanticscholar":
                # Semantic Scholar already performs semantic expansion and its
                # public quota is the tightest connector budget.
                variants = (
                    variants[:1]
                    + [
                        item for item in variants[1:] if item.get("lane") == "relations"
                    ][:2]
                )
            elif source != "model_web":
                # Keep long-running topics under public connector rate limits
                # while retaining the full plan for the most recall-oriented
                # source. The primary query always receives full pagination.
                if search_depth == "deep":
                    variant_caps = {
                        "arxiv": 12,
                        "openalex": 10,
                        "crossref": 8,
                        "europepmc": 6,
                        "dblp": 8,
                        "openreview": 8,
                    }
                elif persistent_search:
                    variant_caps = {
                        "arxiv": 6,
                        "openalex": 5,
                        "crossref": 4,
                        "europepmc": 3,
                        "dblp": 4,
                        "openreview": 4,
                    }
                else:
                    variant_caps = {
                        "arxiv": 3,
                        "openalex": 3,
                        "crossref": 2,
                        "europepmc": 2,
                        "dblp": 2,
                        "openreview": 2,
                    }
                cap = variant_caps.get(source, 4)
                relation_variants = [
                    item for item in variants[1:] if item.get("lane") == "relations"
                ][:2]
                regular_variants = [
                    item for item in variants[1:] if item.get("lane") != "relations"
                ]
                variants = [variants[0], *relation_variants, *regular_variants][:cap]

            def fetch_variant(variant_index, variant):
                prefetched = source_prefetch.get(source)
                if prefetched is not None:
                    if prefetched.get("error"):
                        raise ValueError(str(prefetched["error"]))
                    return prefetched.get("batches", [])[variant_index]
                variant_records = []
                if source == "model_web":
                    return source_prefetch.get("model_web", {}).get("batches", [[]])[
                        variant_index
                    ]
                # Expansion queries add recall, while the primary query keeps
                # the user's normal pagination and saved-query depth.
                variant_pages = max_pages if variant_index == 0 else 1
                variant_fetch_size = (
                    fetch_size if variant_index == 0 else min(fetch_size, 30)
                )
                for page in range(variant_pages):
                    previous_identity_keys = {
                        _services().candidate_identity(item) for item in variant_records
                    }
                    try:
                        batch = _services().SOURCE_SEARCH[source](
                            variant["query"],
                            variant_fetch_size,
                            page * variant_fetch_size,
                        )
                    except TypeError:
                        # Keep compatibility with simple test/custom connectors
                        # that still expose the original two-argument signature.
                        batch = _services().SOURCE_SEARCH[source](
                            variant["query"], variant_fetch_size
                        )
                    batch = [
                        dict(
                            item,
                            _search_variant=variant["id"],
                            _search_query=variant["query"],
                            _source_rank=(page * variant_fetch_size) + item_index + 1,
                            _variant_rank=variant_index + 1,
                        )
                        for item_index, item in enumerate(batch)
                    ]
                    variant_records.extend(batch)
                    # A connector page can be full while containing many
                    # aliases of the same work.  Only request another page
                    # when the unique count is still below the requested
                    # page size; a normal unique first page keeps the fast
                    # one-request path.
                    unique_count = len(
                        _services().merge_search_records(variant_records)[0]
                    )
                    if (
                        variant_index == 0
                        and unique_count >= min(max(int(limit), 1), variant_fetch_size)
                        and not (setup.refill_repeats or persistent_search)
                        and len(batch) >= variant_fetch_size
                    ):
                        break
                    if (
                        setup.refill_repeats
                        and repeat_unseen_count(variant_records) >= requested_unseen
                        and len(batch) >= variant_fetch_size
                    ):
                        break
                    # Do not keep asking a connector that returned the same
                    # page despite an offset. This is especially important
                    # for the bounded repeat-refill budget above.
                    if page > 0 and not (
                        {_services().candidate_identity(item) for item in batch}
                        - previous_identity_keys
                    ):
                        break
                    duplicate_count = len(variant_records) - unique_count
                    if (
                        len(batch) < variant_fetch_size
                        and duplicate_count <= 0
                        and not (setup.refill_repeats and variant_index == 0)
                    ):
                        break
                return variant_records

            # Network expansion lanes are independent. Running them together
            # cuts wall-clock time without changing result order or ranking.
            with _services().ThreadPoolExecutor(
                max_workers=min(4, max(1, len(variants))),
                thread_name_prefix="search-variant",
            ) as pool:
                variant_futures = [
                    pool.submit(fetch_variant, variant_index, variant)
                    for variant_index, variant in enumerate(variants)
                ]
                variant_batches = [future.result() for future in variant_futures]
            for variant, variant_records in zip(variants, variant_batches):
                all_records.extend(variant_records)
                variant_filtered = [
                    record
                    for record in variant_records
                    if _services().record_matches_search_options(record, setup.search_options)
                ]
                variant_details.append(
                    {
                        "id": variant["id"],
                        "query": variant["query"],
                        "kind": variant.get("kind", ""),
                        "source": variant.get("source", source),
                        "fetched": len(variant_records),
                        "accepted": len(variant_filtered),
                        "filtered": len(variant_records) - len(variant_filtered),
                    }
                )
            # Normal connector searches also need a transparent history lane.
            # Use canonical identities for the shortage check; raw connector
            # row counts include aliases and cross-index duplicates.  The
            # helper only reuses candidates from this exact research intent,
            # skips explicit user rejections, and never imports anything.
            if (
                source != "model_web"
                and not history_fill_attempted
                and (
                    not setup.search_options.get("suppress_seen")
                    or setup.search_options.get("fill_repeats")
                )
                and setup.prior_memory_count > 0
            ):
                history_fill_attempted = True
                # Apply the user's constraints before deciding whether the
                # current page is full.  Counting raw connector rows here is
                # misleading: a page can contain many duplicates or papers
                # later rejected by year/venue/exclusion filters, which used
                # to suppress the history refill and leave a short result
                # page even though eligible historical papers were available.
                existing_history_records = [
                    item
                    for item in [*all_records, *search_observations.values()]
                    if isinstance(item, dict)
                    and _services().record_matches_search_options(item, setup.search_options)
                ]
                unique_existing = {
                    _services().candidate_identity(item)
                    for item in existing_history_records
                    if isinstance(item, dict) and _services().candidate_identity(item)
                }
                try:
                    requested_unique = min(100, max(1, int(limit)))
                except (TypeError, ValueError):
                    requested_unique = 25
                if len(unique_existing) < requested_unique:
                    history_rows = _services()._model_search_history_fill(
                        setup.intent_key,
                        setup.q,
                        existing_history_records,
                        requested_unique,
                        {**setup.search_options, "search_backend": "connectors"},
                    )
                    for history_index, history_item in enumerate(history_rows, start=1):
                        # Keep the current connector as the execution source;
                        # the record's ``sources``/provider metadata still
                        # preserves where the historical candidate originated.
                        history_item["source"] = source
                        history_item["_search_variant"] = "history-fill"
                        history_item["_search_query"] = setup.q
                        history_item["_source_rank"] = 1000 + history_index
                        history_item["_variant_rank"] = len(variants) + 1
                        history_item["selection_backfilled"] = True
                        all_records.append(history_item)
                    if history_rows:
                        variant_details.append(
                            {
                                "id": "history-fill",
                                "query": setup.q,
                                "kind": "同一研究意图的历史候选补位",
                                "source": source,
                                "fetched": len(history_rows),
                                "accepted": len(history_rows),
                                "filtered": 0,
                            }
                        )
            raw_count = sum(item["fetched"] for item in variant_details)
            # Count filtering on the unique candidate set, so a variant that
            # returns the same work does not inflate the filter diagnostics.
            unique_raw_records, variant_duplicates = _services().merge_search_records(
                all_records
            )
            records = [
                record
                for record in unique_raw_records
                if _services().record_matches_search_options(record, setup.search_options)
            ]
            filtered_count = len(unique_raw_records) - len(records)
            raw_paths = {}
            for item in all_records:
                path = {
                    "variant_id": item.get("_search_variant", "primary"),
                    "query": item.get("_search_query", setup.q),
                    "source": source,
                }
                identity = _services().candidate_identity(item)
                if path not in raw_paths.setdefault(identity, []):
                    raw_paths[identity].append(path)
            path_by_identity = {}
            for item in unique_raw_records:
                identities = [
                    _services().candidate_identity(item),
                    *(item.get("_dedup_aliases") or []),
                ]
                paths = []
                for identity in identities:
                    for path in raw_paths.get(identity, []):
                        if path not in paths:
                            paths.append(path)
                path_by_identity[_services().candidate_identity(item)] = paths
            src_new = src_dup = 0
            for rec in records:
                if setup.persist_results:
                    paper_id, created = _services().upsert_paper(
                        rec,
                        source=source,
                        external_id=rec.get("external_id", ""),
                        query_id=query_id,
                        raw=rec,
                    )
                else:
                    candidate = _services().normalize_record(rec)
                    identity_keys = _services().candidate_identity_variants(
                        {**candidate, **rec}
                    )
                    existing_candidate_id = next(
                        (
                            candidate_identity_to_id[key]
                            for key in identity_keys
                            if key in candidate_identity_to_id
                        ),
                        "",
                    )
                    paper_id = existing_candidate_id or "candidate:" + str(
                        _services().uuid.uuid4()
                    )
                    created = not bool(existing_candidate_id)
                    if existing_candidate_id:
                        candidate = candidate_records[existing_candidate_id]
                        for field in (
                            "abstract",
                            "authors",
                            "venue",
                            "url",
                            "pdf_url",
                            "doi",
                            "arxiv_id",
                            "year",
                        ):
                            if not candidate.get(field) and rec.get(field):
                                candidate[field] = rec[field]
                        source_records = (
                            rec.get("provider_sources")
                            if source == "model_web"
                            else None
                        )
                        source_records = (
                            source_records
                            if isinstance(source_records, list) and source_records
                            else [
                                {
                                    "source": source,
                                    "external_id": rec.get("external_id", ""),
                                    "fetched_at": setup.started,
                                }
                            ]
                        )
                        candidate.setdefault("sources", [])
                        for source_record in source_records:
                            source_record = {**source_record, "fetched_at": setup.started}
                            if source_record not in candidate["sources"]:
                                candidate["sources"].append(source_record)
                        if rec.get("_dedup_aliases"):
                            candidate["_dedup_aliases"] = list(
                                dict.fromkeys(
                                    [
                                        *(candidate.get("_dedup_aliases") or []),
                                        *rec["_dedup_aliases"],
                                    ]
                                )
                            )
                    else:
                        source_records = (
                            rec.get("provider_sources")
                            if source == "model_web"
                            else None
                        )
                        source_records = (
                            source_records
                            if isinstance(source_records, list) and source_records
                            else [
                                {
                                    "source": source,
                                    "external_id": rec.get("external_id", ""),
                                    "fetched_at": setup.started,
                                }
                            ]
                        )
                        candidate.update(
                            {
                                "id": paper_id,
                                "source": source,
                                "external_id": rec.get("external_id", ""),
                                "raw": _services().compact_search_raw(rec),
                                "sources": [
                                    {**source_record, "fetched_at": setup.started}
                                    for source_record in source_records
                                ],
                            }
                        )
                    for field in (
                        "source_citations",
                        "source_attested",
                        "verified",
                        "verification_status",
                        "verification_sources",
                        "discovery_only",
                        "tavily_score",
                        "academic_source",
                        "relevance_reason",
                        "model_provider",
                        "model",
                        "provider_sources",
                        "search_paths",
                        "selection_backfilled",
                        "history_quality_accepted",
                    ):
                        if field in rec and not candidate.get(field):
                            candidate[field] = rec[field]
                    if rec.get("relevance_label"):
                        candidate.setdefault(
                            "model_relevance_label", rec["relevance_label"]
                        )
                    candidate["candidate_key"] = _services().candidate_identity(
                        candidate
                    )
                    candidate["intent_key"] = setup.intent_key
                    candidate_records[paper_id] = candidate
                    for identity_key in _services().candidate_identity_variants(
                        {**candidate, **rec}
                    ):
                        candidate_identity_to_id[identity_key] = paper_id
                observed = search_observations.setdefault(paper_id, dict(rec))
                for field in ("doi", "arxiv_id", "title", "year", "_dedup_aliases"):
                    if not observed.get(field) and rec.get(field):
                        observed[field] = rec[field]
                if rec.get("_dedup_aliases"):
                    observed["_dedup_aliases"] = list(
                        dict.fromkeys(
                            [
                                *(observed.get("_dedup_aliases") or []),
                                *rec["_dedup_aliases"],
                            ]
                        )
                    )
                if paper_id not in paper_ids:
                    paper_ids.append(paper_id)
                try:
                    citation_count = max(0, int(rec.get("citation_count") or 0))
                except (TypeError, ValueError):
                    citation_count = 0
                signal = paper_signals.setdefault(
                    paper_id,
                    {"citation_count": 0, "open_access": False, "lanes": set()},
                )
                signal["citation_count"] = max(signal["citation_count"], citation_count)
                signal["open_access"] = bool(
                    signal["open_access"] or rec.get("open_access")
                )
                signal["lanes"].add(
                    next(
                        (
                            item.get("lane")
                            for item in variants
                            if item.get("id") == rec.get("_search_variant")
                        ),
                        "primary",
                    )
                )
                rank_signal = paper_rank_signals.setdefault(
                    paper_id, {"rrf": [], "sources": set(), "variants": set()}
                )
                provider_names = (
                    [
                        item.get("source")
                        for item in rec.get("provider_sources", [])
                        if isinstance(item, dict) and item.get("source")
                    ]
                    if source == "model_web"
                    else [source]
                )
                for provider_name in dict.fromkeys(provider_names or [source]):
                    rank_signal["rrf"].append(
                        {
                            "source": provider_name,
                            "rank": int(rec.get("_source_rank") or 1),
                            "weight": _services().SOURCE_RANK_WEIGHTS.get(
                                provider_name, 0.8
                            ),
                        }
                    )
                    rank_signal["sources"].add(provider_name)
                rank_signal["variants"].add(rec.get("_search_variant", "primary"))
                existing_paths = paper_paths.setdefault(paper_id, [])
                for path in path_by_identity.get(
                    _services().candidate_identity(rec), []
                ):
                    if path not in existing_paths:
                        existing_paths.append(path)
                result_created[paper_id] = (
                    result_created.get(paper_id, False) or created
                )
                if created:
                    src_new += 1
                else:
                    src_dup += 1
            new_count += src_new
            duplicate_count += src_dup + variant_duplicates
            # A model-web run can return useful candidates while one of its
            # optional connectors is rate-limited.  Preserve the candidates,
            # but expose that source-level degradation in the durable detail
            # instead of silently reporting every source as healthy.
            detail_error = (
                "；".join(model_web_source_errors[:4])
                if source == "model_web" and model_web_source_errors
                else ""
            )
            detail_status = "partial" if detail_error else "ok"
            details.append(
                {
                    "source": source,
                    "status": detail_status,
                    "fetched": raw_count,
                    "accepted": len(records),
                    "filtered": filtered_count,
                    "variant_duplicates": variant_duplicates,
                    "new": src_new,
                    "duplicates": src_dup + variant_duplicates,
                    "variants": variant_details,
                    "error": detail_error,
                }
            )
        except (
            _services().HTTPError,
            _services().URLError,
            TimeoutError,
            _services().ET.ParseError,
            ValueError,
            OSError,
        ) as exc:
            message = f"{source}: {str(exc)[:500]}"
            errors.append(message)
            details.append(
                {
                    "source": source,
                    "status": "error",
                    "fetched": 0,
                    "accepted": 0,
                    "filtered": 0,
                    "variant_duplicates": 0,
                    "new": 0,
                    "duplicates": 0,
                    "variants": [],
                    "error": message,
                }
            )
    return SimpleNamespace(**(vars(setup) | locals()))

