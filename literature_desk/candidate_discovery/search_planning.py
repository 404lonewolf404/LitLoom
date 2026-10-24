"""Search query planning, options, and source prefetch helpers."""

from __future__ import annotations

from ..services import app_services as _services


def library_relation_variants(query: str, limit: int = 3) -> list[dict]:
    """Build bounded title seeds from the local library for neighbourhood search."""
    if not _services().clean_text(query, 1000):
        return []
    with _services().DB_LOCK, _services().db_session() as db:
        rows = [
            _services().paper_dict(row)
            for row in db.execute(
                "SELECT * FROM papers WHERE status <> 'excluded' ORDER BY updated_at DESC LIMIT 400"
            )
        ]
    ranked = []
    for paper in rows:
        overlap = _services().text_overlap(query, _services().paper_text(paper))
        if overlap["score"] >= 18 and paper.get("title"):
            ranked.append((overlap["score"], paper))
    ranked.sort(
        key=lambda item: (-item[0], item[1].get("updated_at", "")), reverse=False
    )
    variants = []
    for index, (score, paper) in enumerate(
        ranked[: max(1, min(int(limit), 5))], start=1
    ):
        variants.append(
            {
                "id": f"relation-{index}",
                "query": _services().clean_text(paper["title"], 500),
                "kind": "馆藏邻域",
                "reason": f"沿本地论文扩展相似、引用与后续工作（主题重合 {score}）",
                "lane": "relations",
            }
        )
    return variants


def library_term_variants(query: str, limit: int = 2) -> list[dict]:
    """Learn terminology from the user's accepted/read library.

    This is a small local expansion pass: it discovers the vocabulary a
    researcher actually uses over time, without sending notes or full text to
    a remote model.  Terms already present in the query are excluded.
    """
    raw = _services().clean_text(query, 1000)
    if not raw:
        return []
    with _services().DB_LOCK, _services().db_session() as db:
        rows = [
            _services().paper_dict(row)
            for row in db.execute(
                "SELECT * FROM papers WHERE status IN ('read','reading','to_read','watching') OR is_favorite=1 ORDER BY updated_at DESC LIMIT 300"
            )
        ]
    query_terms = _services().expanded_relevance_terms(raw)
    counter = {}
    for paper in rows:
        for term in _services().relevance_tokens(_services().paper_text(paper)):
            if (
                term in query_terms
                or len(term) < 3
                or term in _services().REL_STOPWORDS
            ):
                continue
            counter[term] = counter.get(term, 0) + 1
    learned = [
        term
        for term, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        if count >= 2
    ][: max(1, min(int(limit), 4))]
    if not learned:
        return []
    return [
        {
            "id": f"memory-{index}",
            "query": _services().clean_text(f"{raw} {term}", 1000),
            "kind": "馆藏术语学习",
            "reason": f"从你的已读/收藏论文学习术语“{term}”，寻找同方向的新表达",
            "lane": "memory",
        }
        for index, term in enumerate(learned, start=1)
    ]


def search_query_plan(
    query: str, sources: list[str], options: dict | None = None
) -> dict:
    """Create a transparent search plan before network calls are made."""
    options = options or {}
    raw = _services().clean_text(query, 1000)
    normalized_options = _services().normalize_search_options(options)
    quoted = [
        _services().clean_text(value, 200)
        for value in _services().re.findall(r'"([^"\n]+)"', raw)
        if _services().clean_text(value, 200)
    ]
    # Keep words inside an exact phrase as concepts as well; the phrase is
    # preserved separately so connectors can explain both signals.
    terms, expanded_terms = _services().search_plan_terms(raw)
    variants = (
        _services().search_query_variants(
            raw,
            max_variants=12 if normalized_options.get("search_depth") == "deep" else 8,
            depth=normalized_options.get("search_depth", "balanced"),
        )
        if normalized_options.get("expand_concepts", True)
        else [
            {
                "id": "primary",
                "query": raw,
                "kind": "原始问题",
                "reason": "按设置仅使用原始检索表达",
                "lane": "primary",
            }
        ]
    )
    if normalized_options.get("expand_relations"):
        relation_variants = _services().library_relation_variants(raw)
        if relation_variants:
            variants = [variants[0], *relation_variants, *variants[1:]]
    if normalized_options.get("adaptive_expansion", True) and normalized_options.get(
        "discovery_mode"
    ) in {"explore", "latest"}:
        learned_variants = _services().library_term_variants(raw)
        if learned_variants:
            variants = [variants[0], *learned_variants, *variants[1:]]
    if normalized_options.get("discovery_mode") == "latest":
        base = _services()._bridge_query_terms(raw)
        variants.append(
            {
                "id": f"variant-{len(variants)}",
                "query": _services().clean_text(f"{base} recent advances", 1000),
                "kind": "前沿入口",
                "reason": "补充近期进展、最新方法和技术演化",
                "lane": "frontier",
            }
        )
    elif normalized_options.get("discovery_mode") == "foundations":
        variants.append(
            {
                "id": f"variant-{len(variants)}",
                "query": _services().clean_text(f"{raw} seminal foundational", 1000),
                "kind": "基础入口",
                "reason": "寻找奠基工作、经典基线和早期定义",
                "lane": "foundations",
            }
        )
    elif normalized_options.get("discovery_mode") == "explore":
        variants.append(
            {
                "id": f"variant-{len(variants)}",
                "query": _services().clean_text(
                    f"{raw} adjacent interdisciplinary", 1000
                ),
                "kind": "跨领域入口",
                "reason": "主动探索相邻学科和可迁移方法",
                "lane": "adjacent",
            }
        )
    # Keep the plan bounded even when relation and memory lanes are both on.
    variants = variants[:16]
    source_strategy = {
        "arxiv": "最新预印本与计算机科学方法",
        "openalex": "跨学科覆盖、引用与出版元数据",
        "crossref": "期刊/会议 DOI 与正式发表记录",
        "europepmc": "医学、生命科学与开放全文线索",
        "semanticscholar": "语义检索、作者与相似工作（受公开配额影响）",
        "dblp": "计算机科学会议、期刊与作者索引",
        "openreview": "开放评审会议与 workshop 的最新投稿",
        "tavily": "联网发现论文线索；结果必须经过学术身份核验",
        "deepseek_search": "外部实时检索；结果必须经过学术身份核验",
    }
    active_sources = [
        source for source in sources if source in _services().SOURCE_SEARCH
    ]
    return {
        "original_query": raw,
        "concept_terms": terms[:24],
        "expanded_concept_terms": expanded_terms[:36],
        "facets": _services().extract_search_facets(raw),
        "exact_phrases": quoted,
        "query_variants": variants,
        "expansion_count": max(0, len(variants) - 1),
        "coverage_layers": ["原始问题"]
        + (["英文桥接/概念别名"] if len(variants) > 1 else [])
        + (
            ["馆藏论文邻域"]
            if normalized_options.get("expand_relations")
            and any(item.get("lane") == "relations" for item in variants)
            else []
        )
        + (
            ["馆藏术语学习"]
            if any(item.get("lane") == "memory" for item in variants)
            else []
        )
        + (
            ["综述/基准/实证入口", "综述/基准/实证/失败分析入口"]
            if normalized_options.get("search_depth") == "deep"
            else []
        )
        + (
            ["前沿/相邻领域入口"]
            if normalized_options.get("discovery_mode") in {"latest", "explore"}
            else []
        ),
        "sources": [
            {"id": source, "role": source_strategy.get(source, "学术元数据来源")}
            for source in active_sources
        ],
        "filters": normalized_options,
        "strategy": "原始问题、概念桥接、领域入口与可选馆藏邻域并行 → 强标识/近题名去重 → 多源 Reciprocal Rank Fusion → 相关性、前沿度、长期新颖性与多样性重排",
        "ranking": {
            "method": "reciprocal-rank-fusion + explainable lexical triage + MMR diversity",
            "source_weights": _services().SOURCE_RANK_WEIGHTS,
        },
    }


def normalize_search_options(options: dict | None) -> dict:
    options = options or {}
    result = {}
    for key in (
        "author",
        "venue",
        "exclude_terms",
        "work_type",
        "sort",
        "search_depth",
        "discovery_mode",
    ):
        value = _services().clean_text(options.get(key, ""), 200)
        if value:
            result[key] = value
    for key in ("year_from", "year_to"):
        value = options.get(key)
        try:
            if value not in (None, ""):
                result[key] = min(2200, max(1800, int(value)))
        except (TypeError, ValueError):
            continue
    if (
        result.get("year_from")
        and result.get("year_to")
        and result["year_from"] > result["year_to"]
    ):
        result["year_from"], result["year_to"] = result["year_to"], result["year_from"]
    if result.get("sort") not in {
        "relevance",
        "newest",
        "oldest",
        "novelty",
        "foundations",
    }:
        result["sort"] = "relevance"
    if result.get("search_depth") not in _services().SEARCH_DEPTHS:
        result["search_depth"] = "balanced"
    if result.get("discovery_mode") not in _services().DISCOVERY_MODES:
        result["discovery_mode"] = "balanced"
    expand_value = options.get("expand_concepts", True)
    if isinstance(expand_value, str):
        expand_value = expand_value.casefold() not in {"0", "false", "off", "no", "否"}
    result["expand_concepts"] = bool(expand_value)
    # Adaptive semantic expansion is on for natural-language questions.  It
    # uses the configured model only as a bounded terminology planner; a
    # missing/failed model always falls back to the local facet planner.
    for key, default in (
        ("suppress_seen", False),
        ("fill_repeats", False),
        ("strict_fresh_only", False),
        ("avoid_repeats", True),
        ("incremental_discovery", True),
        ("include_open_access", False),
        ("expand_relations", False),
        ("diversity", True),
        ("adaptive_expansion", True),
        ("ai_expand", True),
        ("auto_add", False),
        ("ai_screen", False),
        ("review_only", False),
        ("model_web_fallback_to_connectors", False),
    ):
        value = options.get(key, default)
        if isinstance(value, str):
            value = value.casefold() not in {"0", "false", "off", "no", "否"}
        result[key] = bool(value)
    # ``strict_fresh_only`` is the one explicit escape hatch for callers that
    # truly need an empty fresh-only page.  Make it dominant over a stale
    # ``fill_repeats`` value carried by an older saved request; otherwise the
    # two flags could silently produce a history-filled page while the caller
    # believed it had requested strict filtering.
    if result["strict_fresh_only"]:
        result["suppress_seen"] = True
        result["fill_repeats"] = False
    # Source connectors are independent network calls.  Keep this enabled by
    # default so one slow provider does not serialize the whole discovery
    # request; callers can disable it when operating under a strict rate limit.
    parallel_sources = options.get("parallel_sources", True)
    if isinstance(parallel_sources, str):
        parallel_sources = parallel_sources.casefold() not in {
            "0",
            "false",
            "off",
            "no",
            "否",
        }
    result["parallel_sources"] = bool(parallel_sources)
    backend = (
        _services()
        .clean_text(options.get("search_backend", "connectors"), 40)
        .casefold()
    )
    result["search_backend"] = (
        "model_web"
        if backend in {"model_web", "model", "web", "模型检索"}
        else "connectors"
    )
    return result


def prepare_user_search_options(options: dict | None) -> dict | None:
    """Canonicalize options received from the browser and saved runs.

    The discovery UI describes ``suppress_seen`` as "prefer new papers".
    Older clients sent that flag without ``fill_repeats`` (or explicitly sent
    it as false), which silently enabled strict fresh-only mode and made a
    normal 20-result page collapse to one or two rows.  Keep the normal user
    workflow full: historical candidates may fill missing slots and are
    visibly marked by the ranking layer.  A caller that really needs an empty
    fresh-only page must opt into the explicit ``strict_fresh_only`` flag.
    """
    # Preserve ``None`` for legacy one-off callers.  ``search_persists_results``
    # intentionally distinguishes an omitted options object from an explicit
    # review-only options object.
    if options is None:
        return None
    raw = dict(options)

    def truthy(value: object) -> bool:
        if isinstance(value, str):
            return value.casefold() not in {"", "0", "false", "off", "no", "否"}
        return bool(value)

    if truthy(raw.get("suppress_seen")) and not truthy(raw.get("strict_fresh_only")):
        raw["fill_repeats"] = True
    return raw


def search_persists_results(options: dict | None, scheduled: bool = False) -> bool:
    """Persist discoveries only after explicit opt-in; scheduled runs stay review-only."""
    normalized = _services().normalize_search_options(options)
    legacy_default = options is None and not scheduled
    return (
        bool(normalized["auto_add"] or legacy_default) and not normalized["review_only"]
    )


def record_matches_search_options(record: dict, options: dict) -> bool:
    exclude_terms = [
        item.casefold()
        for item in _services().re.split(r"[,;\n]+", options.get("exclude_terms", ""))
        if item.strip()
    ]
    if exclude_terms:
        haystack = " ".join(
            str(record.get(key, "") or "")
            for key in ("title", "abstract", "authors", "venue")
        ).casefold()
        if any(term in haystack for term in exclude_terms):
            return False
    year = record.get("year")
    if options.get("year_from") and (not year or int(year) < options["year_from"]):
        return False
    if options.get("year_to") and (not year or int(year) > options["year_to"]):
        return False
    author = options.get("author", "").casefold()
    if author and author not in str(record.get("authors", "")).casefold():
        return False
    venue = options.get("venue", "").casefold()
    if venue and venue not in str(record.get("venue", "")).casefold():
        return False
    work_type = options.get("work_type", "").casefold()
    actual_type = str(record.get("type", "")).casefold()
    source = str(record.get("source", "")).casefold()
    if work_type == "journal" and not any(
        token in actual_type for token in ("journal", "article")
    ):
        return False
    if work_type == "conference" and not any(
        token in actual_type or token in str(record.get("venue", "")).casefold()
        for token in ("conference", "proceeding", "symposium", "workshop")
    ):
        return False
    if (
        work_type == "review"
        and "review" not in actual_type
        and "review" not in str(record.get("title", "")).casefold()
    ):
        return False
    if (
        work_type == "preprint"
        and "preprint" not in actual_type
        and source != "arxiv"
        and "posted" not in actual_type
    ):
        return False
    if options.get("include_open_access") and not record.get("open_access"):
        return False
    return True


def rank_fusion_score(signal: dict) -> int:
    """Turn per-source ranks into an explainable reciprocal-rank score."""
    contributions = signal.get("rrf", [])
    if not contributions:
        return 0
    total = sum(
        float(item.get("weight", 1.0)) / (1.0 + max(0, int(item.get("rank", 0))) / 10.0)
        for item in contributions
    )
    # A first-place result from one reliable source is useful; confirmation by
    # a second source quickly raises confidence but never dominates relevance.
    score = round(min(100.0, total * 45.0))
    return max(0, score)


def diversify_results(results: list[dict], limit: int = 80) -> list[dict]:
    """Apply bounded MMR re-ranking so a page covers multiple sub-areas.

    This is lexical by design and therefore works offline.  It uses the same
    normalized terms as the explainable ranker, so the reason for a broadened
    result remains inspectable instead of hiding a black-box embedding.
    """
    if len(results) < 3:
        return results
    candidates = list(results)
    selected = []
    first_pass = min(max(int(limit), 20), len(candidates))
    while candidates and len(selected) < first_pass:
        best_index, best_value, best_penalty = 0, -(10**9), 0
        for index, item in enumerate(candidates):
            base = float(
                item.get("discovery_score") or item.get("relevance_score") or 0
            )
            penalty = 0.0
            if selected:
                similarities = [
                    _services().text_overlap(
                        _services().paper_text(item), _services().paper_text(other)
                    )["score"]
                    for other in selected[-18:]
                ]
                penalty = max(similarities or [0]) * 0.30
                if item.get("venue") and item.get("venue") == selected[-1].get("venue"):
                    penalty += 4
            value = base - penalty
            if value > best_value:
                best_index, best_value, best_penalty = index, value, penalty
        item = candidates.pop(best_index)
        item["diversity_penalty"] = round(best_penalty, 2)
        item["diversity_score"] = round(
            max(
                0.0,
                float(item.get("discovery_score") or item.get("relevance_score") or 0)
                - best_penalty,
            ),
            2,
        )
        selected.append(item)
    # The tail is still returned, preserving the existing API's full result
    # set while making the visible top of the page less redundant.
    selected.extend(candidates)
    return selected


def _prioritize_ai_search_variants(plan: dict) -> list[dict]:
    variants = plan.get("query_variants") or [
        {
            "id": "primary",
            "query": plan.get("query", ""),
            "kind": "原始问题",
            "lane": "primary",
        }
    ]
    if not (plan.get("ai_expansion") or {}).get("enabled"):
        return variants
    # Keep the original question, then spend each connector's bounded request
    # budget on expressions the model generated before local rule variants.
    primary = [item for item in variants if item.get("lane") == "primary"][:1]
    ai_variants = [
        item for item in variants if item.get("lane") in {"ai", "ai-adjacent"}
    ]
    other_variants = [
        item
        for item in variants
        if item.get("lane") not in {"primary", "ai", "ai-adjacent"}
    ]
    return [*primary, *ai_variants, *other_variants]


def _prefetch_source_variants(
    source: str,
    plan: dict,
    limit: int,
    persistent_search: bool,
    search_depth: str,
    refill_repeats: bool = False,
    seen_identities: set[str] | None = None,
) -> dict:
    """Fetch one connector's independent query lanes for parallel discovery.

    The main run still performs all filtering, deduplication, persistence and
    ranking in one deterministic pass.  This helper only moves network I/O
    ahead of that pass, so a slow connector cannot hold up every other source.
    """
    fetch_size = 50 if persistent_search else min(max(int(limit), 1), 50)
    plan_options = _services().normalize_search_options(
        plan.get("search_options") if isinstance(plan, dict) else {}
    )
    # A strict fresh-only search needs another connector page before it can
    # honestly report an empty inbox. Keep the extra request bounded and only
    # enable it when the user asked for semantic/AI expansion.
    widen_for_fresh = bool(
        plan_options.get("suppress_seen")
        and (plan_options.get("ai_expand") or plan_options.get("ai_screen"))
    )
    # A repeated topic gets bounded primary continuation pages so filtering
    # historical papers does not silently shrink the user's requested result
    # count. The first search keeps the existing one-page latency budget.
    # Keep one extra page available for the common case where a provider's
    # first page is mostly cross-index duplicates.  The fetch loop exits
    # immediately once the unique count reaches the requested page size, so
    # this does not add a request for ordinary searches.
    # A repeated topic can have several provider pages occupied by records
    # already seen in this research question. Fetch enough continuation pages
    # to find a full page of *unseen* works, rather than stopping after a page
    # of historical aliases. The no-progress guard below still stops a
    # connector that ignores offsets from being queried repeatedly. New topics
    # retain the fast one-page path.
    max_pages = (
        8 if refill_repeats else (3 if persistent_search or widen_for_fresh else 2)
    )
    prior_seen = set(seen_identities or ())
    try:
        requested_unseen = min(100, max(1, int(limit)))
    except (TypeError, ValueError):
        requested_unseen = 25

    def unseen_count(records: list[dict]) -> int:
        if not prior_seen:
            return len(records)
        count = 0
        for record in _services().merge_search_records(records)[0]:
            identities = _services().candidate_identity_variants(record)
            if not identities or not identities.intersection(prior_seen):
                count += 1
        return count

    variants = _services()._prioritize_ai_search_variants(plan)
    if source == "semanticscholar":
        variants = (
            variants[:1]
            + [item for item in variants[1:] if item.get("lane") == "relations"][:2]
        )
    else:
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
        variant_records = []
        variant_pages = max_pages if variant_index == 0 else 1
        variant_fetch_size = fetch_size if variant_index == 0 else min(fetch_size, 30)
        for page in range(variant_pages):
            previous_identity_keys = {
                _services().candidate_identity(item) for item in variant_records
            }
            try:
                batch = _services().SOURCE_SEARCH[source](
                    variant["query"], variant_fetch_size, page * variant_fetch_size
                )
            except TypeError:
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
            if not batch:
                break
            # Some custom/relay connectors silently ignore offset. Once a
            # refill page contributes no new identity, stop instead of
            # spending the remaining refill budget on the same records.
            if page > 0 and not (
                {_services().candidate_identity(item) for item in batch}
                - previous_identity_keys
            ):
                break
            unique_count = len(_services().merge_search_records(variant_records)[0])
            duplicate_count = len(variant_records) - unique_count
            # On repeat searches the relevant stopping condition is the
            # number of unseen canonical works, not raw unique rows. A first
            # page may be completely historical while a later page contains
            # the new papers the user asked for.
            if (
                refill_repeats
                and unseen_count(variant_records) >= requested_unseen
                and len(batch) >= variant_fetch_size
            ):
                break
            if (
                len(batch) < variant_fetch_size
                and duplicate_count <= 0
                and not (refill_repeats and variant_index == 0)
            ):
                break
            if (
                unique_count >= min(max(int(limit), 1), variant_fetch_size)
                and not (refill_repeats or persistent_search)
                and len(batch) >= variant_fetch_size
            ):
                break
        return variant_records

    with _services().ThreadPoolExecutor(
        max_workers=min(4, max(1, len(variants))),
        thread_name_prefix="search-source-variant",
    ) as pool:
        batches = list(pool.map(lambda args: fetch_variant(*args), enumerate(variants)))
    details = [
        {
            "id": variant["id"],
            "query": variant["query"],
            "kind": variant.get("kind", ""),
            "fetched": len(records),
            "accepted": len(records),
            "filtered": 0,
        }
        for variant, records in zip(variants, batches)
    ]
    return {"variants": variants, "batches": batches, "details": details}


