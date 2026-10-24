"""OpenAlex and Semantic Scholar recommendation workflows."""

from __future__ import annotations

import sqlite3

from ..services import app_services as _services

def _openalex_work_id(value: str) -> str:
    value = _services().clean_text(value or "", 300).rstrip("/")
    match = _services().re.search(r"(?:^|/)(W\d+)$", value, _services().re.I)
    return match.group(1).upper() if match else ""


def _openalex_seed_ids(paper_ids: list[str]) -> list[str]:
    paper_ids = list(
        dict.fromkeys(_services().clean_text(value, 80) for value in paper_ids if value)
    )[:20]
    if not paper_ids:
        return []
    placeholders = ",".join("?" for _ in paper_ids)
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            f"SELECT external_id FROM source_records WHERE source='openalex' AND paper_id IN ({placeholders}) ORDER BY fetched_at DESC",
            paper_ids,
        ).fetchall()
    return list(
        dict.fromkeys(
            work_id
            for row in rows
            if (work_id := _services()._openalex_work_id(row["external_id"]))
        )
    )[:3]


def _semantic_scholar_seed_ids(paper_ids: list[str]) -> list[str]:
    """Resolve stable Semantic Scholar seed identifiers for recommendations.

    Semantic Scholar accepts its own paper ID and common scholarly identifiers
    such as DOI and arXiv ID. Prefer the provider ID already attached to a
    paper, then fall back to DOI/arXiv so imported papers can still use the
    provider's recommendation model without a local reimplementation.
    """
    paper_ids = list(
        dict.fromkeys(_services().clean_text(value, 80) for value in paper_ids if value)
    )[:20]
    if not paper_ids:
        return []
    placeholders = ",".join("?" for _ in paper_ids)
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            f"SELECT p.doi,p.arxiv_id,s.external_id FROM papers p "
            f"LEFT JOIN source_records s ON s.paper_id=p.id AND s.source='semanticscholar' "
            f"WHERE p.id IN ({placeholders}) ORDER BY s.fetched_at DESC",
            paper_ids,
        ).fetchall()
    seeds = []
    for row in rows:
        for value in (row["external_id"], row["doi"], row["arxiv_id"]):
            value = _services().clean_text(value, 300)
            if value and value not in seeds:
                seeds.append(value)
                break
        if len(seeds) >= _services().SEMANTIC_RECOMMENDATION_MAX_SEEDS:
            break
    return seeds


def _semantic_recommendation_record(item: dict, rank: int, seed_count: int = 1) -> dict:
    """Normalize one Semantic Scholar recommendation without inventing data."""
    external_ids = item.get("externalIds") or {}
    doi = _services().norm_doi(external_ids.get("DOI") or item.get("doi") or "")
    arxiv_id = _services().norm_arxiv(
        external_ids.get("ArXiv") or item.get("arxiv_id") or ""
    )
    paper_id = _services().clean_text(item.get("paperId") or item.get("id") or "", 300)
    title = _services().clean_text(item.get("title"), 1000)
    if not title:
        return {}
    authors = ", ".join(
        _services().clean_text(
            author.get("name") if isinstance(author, dict) else author, 240
        )
        for author in (item.get("authors") or [])[:12]
        if _services().clean_text(
            author.get("name") if isinstance(author, dict) else author, 240
        )
    )
    url = _services()._model_search_url(item.get("url"))
    if not url and paper_id:
        url = f"https://www.semanticscholar.org/paper/{_services().quote(paper_id, safe='')}"
    open_pdf = item.get("openAccessPdf") or {}
    score = min(
        100,
        max(40, 100 - (max(1, int(rank)) - 1) * 3)
        + min(10, max(0, seed_count - 1) * 5),
    )
    return {
        "title": title,
        "authors": authors,
        "year": item.get("year"),
        "abstract": _services().clean_text(item.get("abstract"), 30000),
        "doi": doi,
        "arxiv_id": arxiv_id,
        "venue": _services().clean_text(item.get("venue"), 1000),
        "url": url,
        "pdf_url": _services()._model_search_url(open_pdf.get("url")),
        "source": "semanticscholar",
        "external_id": paper_id or doi or arxiv_id or url,
        "open_access": bool(open_pdf.get("url")),
        "citation_count": int(item.get("citationCount") or 0),
        "recommendation_provider": "Semantic Scholar",
        "recommendation_score": score,
        "recommendation_rank": max(1, int(rank)),
        "recommendation_reasons": ["Semantic Scholar 官方推荐模型召回"],
    }


def _load_semantic_scholar_recommendations(seed_ids: list[str]) -> dict:
    """Use Semantic Scholar's hosted recommendation model for reading seeds."""
    seeds = list(
        dict.fromkeys(
            _services().clean_text(value, 300)
            for value in (seed_ids or [])
            if _services().clean_text(value, 300)
        )
    )[: _services().SEMANTIC_RECOMMENDATION_MAX_SEEDS]
    if not seeds:
        return {
            "provider": "Semantic Scholar",
            "by_semantic_id": {},
            "by_doi": {},
            "external_candidates": [],
            "related_count": 0,
            "seed_count": 0,
            "errors": [],
        }

    def fetch(seed: str):
        endpoint = (
            "https://api.semanticscholar.org/recommendations/v1/papers/for/"
            + _services().quote(seed, safe="")
        )
        endpoint += "?" + _services().urlencode(
            {
                "limit": _services().SEMANTIC_RECOMMENDATION_MAX_RESULTS,
                "fields": "paperId,title,abstract,year,authors,venue,externalIds,url,openAccessPdf,citationCount",
            }
        )
        return _services().http_json(endpoint, timeout=12)

    merged = {}
    errors = []
    with _services().ThreadPoolExecutor(
        max_workers=min(_services().SEMANTIC_RECOMMENDATION_MAX_SEEDS, len(seeds)),
        thread_name_prefix="semantic-recommend",
    ) as pool:
        futures = {pool.submit(fetch, seed): seed for seed in seeds}
        for future in _services().as_completed(futures):
            seed = futures[future]
            try:
                payload = future.result()
            except Exception as exc:
                errors.append(f"{seed}: {_services().clean_text(str(exc), 240)}")
                continue
            rows = payload.get("recommendedPapers") if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                rows = (
                    payload.get("data")
                    if isinstance(payload, dict)
                    and isinstance(payload.get("data"), list)
                    else []
                )
            for rank, item in enumerate(
                rows[: _services().SEMANTIC_RECOMMENDATION_MAX_RESULTS], start=1
            ):
                if not isinstance(item, dict):
                    continue
                record = _services()._semantic_recommendation_record(
                    item, rank, len(seeds)
                )
                if not record:
                    continue
                identity = _services().candidate_identity(record)
                existing = merged.get(identity)
                if not existing or (
                    record.get("recommendation_score", 0),
                    len(record.get("abstract", "")),
                ) > (
                    existing.get("recommendation_score", 0),
                    len(existing.get("abstract", "")),
                ):
                    merged[identity] = record
    candidates = sorted(
        merged.values(),
        key=lambda item: (
            -int(item.get("recommendation_score") or 0),
            int(item.get("recommendation_rank") or 0),
            item.get("title", ""),
        ),
    )[: _services().SEMANTIC_RECOMMENDATION_MAX_RESULTS]
    by_semantic_id = {}
    by_doi = {}
    for item in candidates:
        if item.get("external_id"):
            by_semantic_id[item["external_id"]] = {
                "score": item["recommendation_score"],
                "rank": item["recommendation_rank"],
                "seed_count": len(seeds),
                "provider": "Semantic Scholar",
            }
        if item.get("doi"):
            by_doi[item["doi"]] = {
                "score": item["recommendation_score"],
                "rank": item["recommendation_rank"],
                "seed_count": len(seeds),
                "provider": "Semantic Scholar",
            }
    return {
        "provider": "Semantic Scholar",
        "by_semantic_id": by_semantic_id,
        "by_doi": by_doi,
        "external_candidates": candidates,
        "related_count": len(candidates),
        "seed_count": len(seeds),
        "errors": errors[:8],
    }


def _load_openalex_recommendations(seed_ids: list[str]) -> dict:
    """Ask OpenAlex for its related-work graph and return bounded rank signals."""
    seed_ids = list(
        dict.fromkeys(
            _services()._openalex_work_id(value)
            for value in seed_ids
            if _services()._openalex_work_id(value)
        )
    )[:3]
    if not seed_ids:
        return {"by_openalex": {}, "by_doi": {}, "seed_count": 0}
    seed_url = "https://api.openalex.org/works?" + _services().urlencode(
        {
            "filter": "ids.openalex:" + "|".join(seed_ids),
            "per-page": len(seed_ids),
            "select": "id,related_works",
        }
    )
    seed_data = _services().http_json(seed_url, timeout=12)
    rank_by_id = {}
    for seed in seed_data.get("results", []):
        seed_work_id = _services()._openalex_work_id(seed.get("id", ""))
        for rank, raw_id in enumerate(seed.get("related_works") or [], start=1):
            work_id = _services()._openalex_work_id(raw_id)
            if not work_id or work_id == seed_work_id:
                continue
            item = rank_by_id.setdefault(work_id, {"rank": rank, "seed_count": 0})
            item["rank"] = min(item["rank"], rank)
            item["seed_count"] += 1
    ordered_ids = sorted(
        rank_by_id, key=lambda work_id: (rank_by_id[work_id]["rank"], work_id)
    )[:50]
    by_doi = {}
    external_candidates = []
    if ordered_ids:
        details_url = "https://api.openalex.org/works?" + _services().urlencode(
            {
                "filter": "ids.openalex:" + "|".join(ordered_ids),
                "per-page": len(ordered_ids),
                "select": "id,title,publication_year,doi,authorships,abstract_inverted_index,primary_location,type,cited_by_count",
            }
        )
        detail_data = _services().http_json(details_url, timeout=12)
        for item in detail_data.get("results", []):
            work_id = _services()._openalex_work_id(item.get("id", ""))
            rank_info = rank_by_id.get(work_id)
            if not rank_info:
                continue
            score = min(
                100,
                max(38, 100 - (rank_info["rank"] - 1) * 4)
                + min(12, (rank_info["seed_count"] - 1) * 6),
            )
            doi = _services().norm_doi(item.get("doi", ""))
            if doi:
                by_doi[doi] = {
                    "score": score,
                    "rank": rank_info["rank"],
                    "seed_count": rank_info["seed_count"],
                }
            record = _services()._openalex_relation_candidate(item, "OpenAlex 相关工作")
            if record.get("title"):
                record["recommendation_score"] = score
                record["recommendation_rank"] = rank_info["rank"]
                record["recommendation_reasons"] = [
                    "OpenAlex 根据已读/收藏论文的相关工作图谱召回"
                ]
                external_candidates.append(record)
    by_openalex = {}
    for work_id, rank_info in rank_by_id.items():
        score = min(
            100,
            max(38, 100 - (rank_info["rank"] - 1) * 4)
            + min(12, (rank_info["seed_count"] - 1) * 6),
        )
        by_openalex[work_id] = {
            "score": score,
            "rank": rank_info["rank"],
            "seed_count": rank_info["seed_count"],
        }
    external_candidates.sort(
        key=lambda item: (
            -item["recommendation_score"],
            item["recommendation_rank"],
            item["title"],
        )
    )
    return {
        "by_openalex": by_openalex,
        "by_doi": by_doi,
        "external_candidates": external_candidates[:50],
        "seed_count": len(seed_ids),
        "related_count": len(by_openalex),
    }


def _merge_recommendation_provider_results(
    openalex: dict | None, semantic: dict | None
) -> dict:
    """Merge provider recommendations while retaining per-provider provenance."""
    openalex = openalex if isinstance(openalex, dict) else {}
    semantic = semantic if isinstance(semantic, dict) else {}
    # Keep the historical OpenAlex-only ``external_candidates`` field for API
    # consumers that still expect it, but also expose one provider-neutral
    # candidate list. The latter lets the UI and MCP clients use both hosted
    # recommendation services without pretending that an S2 result came from
    # OpenAlex.
    openalex_candidates = []
    all_candidates = []
    seen = set()
    for provider_name, provider_candidates in (
        ("OpenAlex", openalex.get("external_candidates") or []),
        ("Semantic Scholar", semantic.get("external_candidates") or []),
    ):
        for item in provider_candidates:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized.setdefault("recommendation_provider", provider_name)
            normalized.setdefault(
                "recommendation_reasons", [f"{provider_name} 官方推荐服务召回"]
            )
            key = _services().candidate_identity(normalized)
            if key in seen:
                continue
            seen.add(key)
            all_candidates.append(normalized)
            if provider_name == "OpenAlex":
                openalex_candidates.append(normalized)
    all_candidates.sort(
        key=lambda item: (
            -int(item.get("recommendation_score") or 0),
            int(item.get("recommendation_rank") or 0),
            item.get("title", ""),
        )
    )
    openalex_candidates.sort(
        key=lambda item: (
            -int(item.get("recommendation_score") or 0),
            int(item.get("recommendation_rank") or 0),
            item.get("title", ""),
        )
    )
    by_doi = {}
    for source in (openalex, semantic):
        for doi, signal in (source.get("by_doi") or {}).items():
            current = by_doi.get(doi)
            if not current or int(signal.get("score") or 0) > int(
                current.get("score") or 0
            ):
                by_doi[doi] = dict(signal)
    return {
        "by_openalex": dict(openalex.get("by_openalex") or {}),
        "by_semantic_id": dict(semantic.get("by_semantic_id") or {}),
        "by_doi": by_doi,
        # Backwards-compatible field used by older integrations.
        "external_candidates": openalex_candidates[
            : _services().SEMANTIC_RECOMMENDATION_MAX_RESULTS
        ],
        # Provider-neutral field for new clients and the LitLoom UI.
        "external_recommendations": all_candidates[
            : _services().SEMANTIC_RECOMMENDATION_MAX_RESULTS
        ],
        "seed_count": max(
            int(openalex.get("seed_count") or 0), int(semantic.get("seed_count") or 0)
        ),
        "related_count": len(all_candidates),
        "providers": {
            "openalex": {
                "status": "ready" if openalex else "unavailable",
                "count": len(openalex.get("external_candidates") or []),
                "errors": list(openalex.get("errors") or []),
            },
            "semanticscholar": {
                "status": (
                    "ready"
                    if semantic
                    and (semantic.get("related_count") or not semantic.get("errors"))
                    else "unavailable"
                ),
                "count": len(semantic.get("external_candidates") or []),
                "errors": list(semantic.get("errors") or []),
            },
        },
    }


def _refresh_openalex_recommendations(
    cache_key: str, seed_ids: list[str], semantic_seed_ids: list[str] | None = None
) -> None:
    """Refresh hosted recommendations in parallel and degrade per provider."""
    semantic_seed_ids = list(semantic_seed_ids or [])
    results = {}
    errors = []
    with _services().ThreadPoolExecutor(
        max_workers=2 if semantic_seed_ids else 1,
        thread_name_prefix="recommend-provider",
    ) as pool:
        futures = {}
        if seed_ids:
            futures[
                pool.submit(_services()._load_openalex_recommendations, seed_ids)
            ] = "OpenAlex"
        if semantic_seed_ids:
            futures[
                pool.submit(
                    _services()._load_semantic_scholar_recommendations,
                    semantic_seed_ids,
                )
            ] = "Semantic Scholar"
        for future in _services().as_completed(futures):
            provider = futures[future]
            try:
                results[provider] = future.result()
            except Exception as exc:
                errors.append(f"{provider}：{_services().clean_text(str(exc), 240)}")
    for provider, result in results.items():
        for detail in (result.get("errors") or []) if isinstance(result, dict) else []:
            errors.append(f"{provider}：{_services().clean_text(detail, 240)}")
    merged = _services()._merge_recommendation_provider_results(
        results.get("OpenAlex"), results.get("Semantic Scholar")
    )
    merged["provider_errors"] = errors[:8]
    with _services().OPENALEX_RECOMMENDATION_CACHE_LOCK:
        previous = _services().OPENALEX_RECOMMENDATION_CACHE.get(cache_key) or {}
    if results or merged.get("external_candidates"):
        state = {
            "status": "ready",
            "rankings": merged,
            "updated_at": _services().now_iso(),
            "at": _services().time.monotonic(),
            "error": "；".join(errors[:4]),
        }
    else:
        state = {
            "status": "unavailable",
            "rankings": previous.get("rankings") or {},
            "updated_at": previous.get("updated_at", ""),
            "at": _services().time.monotonic(),
            "retry_at": _services().time.monotonic()
            + _services().OPENALEX_RECOMMENDATION_ERROR_BACKOFF_SECONDS,
            "error": "；".join(errors[:4]) or "推荐服务暂时不可用",
        }
    with _services().OPENALEX_RECOMMENDATION_CACHE_LOCK:
        _services().OPENALEX_RECOMMENDATION_CACHE[cache_key] = state


def _openalex_recommendation_snapshot(seed_paper_ids: list[str]) -> dict:
    seed_ids = _services()._openalex_seed_ids(seed_paper_ids)
    semantic_seed_ids = _services()._semantic_scholar_seed_ids(seed_paper_ids)
    if not seed_ids and not semantic_seed_ids:
        return {
            "status": "not_applicable",
            "rankings": {},
            "provider": "OpenAlex + Semantic Scholar",
            "message": "阅读记录中还没有可用于外部推荐的学术标识，暂用本地研究画像推荐。",
        }
    provider_label = " + ".join(
        name
        for name, enabled in (
            ("OpenAlex", bool(seed_ids)),
            ("Semantic Scholar", bool(semantic_seed_ids)),
        )
        if enabled
    )
    cache_key = ",".join(sorted(seed_ids))
    if semantic_seed_ids:
        cache_key = "oa:" + cache_key + "|s2:" + ",".join(sorted(semantic_seed_ids))
    now = _services().time.monotonic()
    with _services().OPENALEX_RECOMMENDATION_CACHE_LOCK:
        cached = _services().OPENALEX_RECOMMENDATION_CACHE.get(cache_key) or {}
        age = now - float(cached.get("at", 0) or 0)
        if cached.get("status") == "running":
            message = (
                "正在并行更新 OpenAlex 与 Semantic Scholar 推荐；已有结果继续生效。"
                if cached.get("rankings")
                else "正在并行获取外部推荐信号。"
            )
            return {
                "status": "running",
                "rankings": cached.get("rankings") or {},
                "provider": provider_label,
                "message": message,
                "updated_at": cached.get("updated_at", ""),
            }
        if (
            cached.get("status") == "ready"
            and age < _services().OPENALEX_RECOMMENDATION_TTL_SECONDS
        ):
            ranking_data = cached.get("rankings") or {}
            message = (
                "已结合 OpenAlex、Semantic Scholar 与本地阅读反馈排序。"
                if ranking_data.get("related_count")
                else "外部推荐暂无可用信号，当前根据本地研究画像推荐。"
            )
            if cached.get("error"):
                message += " 部分推荐服务暂不可用，已保留可用结果。"
            return {
                "status": "ready",
                "rankings": ranking_data,
                "provider": provider_label,
                "message": message,
                "updated_at": cached.get("updated_at", ""),
            }
        if cached.get("status") == "unavailable" and now < float(
            cached.get("retry_at", 0) or 0
        ):
            ranking_data = cached.get("rankings") or {}
            message = (
                "外部推荐服务暂不可用，保留上次信号并结合本地反馈。"
                if ranking_data.get("related_count")
                else "外部推荐服务暂不可用，当前使用本地研究画像推荐。"
            )
            return {
                "status": "unavailable",
                "rankings": ranking_data,
                "provider": provider_label,
                "message": message,
                "error": cached.get("error", ""),
                "updated_at": cached.get("updated_at", ""),
            }
        _services().OPENALEX_RECOMMENDATION_CACHE[cache_key] = {
            "status": "running",
            "rankings": cached.get("rankings") or {},
            "updated_at": cached.get("updated_at", ""),
            "at": now,
            "error": "",
        }
    with _services().OPENALEX_RECOMMENDATION_EXECUTOR_LOCK:
        if _services().OPENALEX_RECOMMENDATION_EXECUTOR is None:
            _services().OPENALEX_RECOMMENDATION_EXECUTOR = (
                _services().ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="openalex-recommend"
                )
            )
        executor = _services().OPENALEX_RECOMMENDATION_EXECUTOR
    try:
        executor.submit(
            _services()._refresh_openalex_recommendations,
            cache_key,
            seed_ids,
            semantic_seed_ids,
        )
    except RuntimeError:
        with _services().OPENALEX_RECOMMENDATION_CACHE_LOCK:
            _services().OPENALEX_RECOMMENDATION_CACHE[cache_key] = {
                "status": "unavailable",
                "rankings": cached.get("rankings") or {},
                "updated_at": cached.get("updated_at", ""),
                "at": _services().time.monotonic(),
                "retry_at": _services().time.monotonic() + 30,
                "error": "推荐任务暂时无法排队",
            }
    return {
        "status": "running",
        "rankings": cached.get("rankings") or {},
        "provider": provider_label,
        "message": "正在并行获取外部推荐信号。",
        "updated_at": cached.get("updated_at", ""),
    }


def _available_openalex_recommendations(rankings: dict, limit: int = 6) -> list[dict]:
    rankings = rankings or {}
    # ``external_recommendations`` is the provider-neutral contract. Fall
    # back to the legacy field so cached snapshots created before this change
    # remain usable after an upgrade.
    candidates = (
        rankings.get("external_recommendations")
        or rankings.get("external_candidates")
        or []
    )
    if not candidates:
        return []
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT p.doi,p.title_key,p.year,s.external_id FROM papers p "
            "LEFT JOIN source_records s ON s.paper_id=p.id WHERE p.status<>'excluded'"
        ).fetchall()
    known_dois = {_services().norm_doi(row["doi"]) for row in rows if row["doi"]}
    known_titles = {
        (row["title_key"], row["year"])
        for row in rows
        if row["title_key"] and row["year"]
    }
    known_openalex = {
        _services()._openalex_work_id(row["external_id"])
        for row in rows
        if row["external_id"]
    }
    known_external_ids = {
        _services().clean_text(row["external_id"], 500).casefold()
        for row in rows
        if row["external_id"]
    }
    selected = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        doi = _services().norm_doi(candidate.get("doi", ""))
        external_id = _services().clean_text(
            candidate.get("openalex_id") or candidate.get("external_id") or "", 500
        )
        work_id = _services()._openalex_work_id(external_id)
        semantic_id = _services().clean_text(
            candidate.get("paperId") or (external_id if not work_id else ""), 500
        )
        title_identity = (
            _services().title_key(candidate.get("title", "")),
            candidate.get("year"),
        )
        if (
            (doi and doi in known_dois)
            or (work_id and work_id in known_openalex)
            or (semantic_id and semantic_id.casefold() in known_external_ids)
            or (external_id and external_id.casefold() in known_external_ids)
            or title_identity in known_titles
        ):
            continue
        item = dict(candidate)
        provider_name = _services().clean_text(
            item.get("recommendation_provider")
            or ("OpenAlex" if work_id else "Semantic Scholar"),
            80,
        )
        item["url"] = item.get("url") or (
            ("https://openalex.org/" + work_id) if work_id else ""
        )
        item["recommendation_reasons"] = list(
            item.get("recommendation_reasons") or [f"{provider_name} 官方推荐服务召回"]
        )
        selected.append(item)
        if len(selected) >= max(1, min(int(limit), 12)):
            break
    return selected


def save_openalex_recommendation(external_id: str) -> dict:
    work_id = _services()._openalex_work_id(external_id)
    local_memory = _services().cached_derived(
        f"research_memory:base:{_services().database_change_token()}",
        lambda: _services()._research_memory(50),
        ttl_seconds=30.0,
    )
    seed_ids = _services()._openalex_seed_ids(
        local_memory.get("_provider_seed_paper_ids", [])
    )
    semantic_seed_ids = _services()._semantic_scholar_seed_ids(
        local_memory.get("_provider_seed_paper_ids", [])
    )
    cache_key = ",".join(sorted(seed_ids))
    if semantic_seed_ids:
        cache_key = "oa:" + cache_key + "|s2:" + ",".join(sorted(semantic_seed_ids))
    candidate = None
    with _services().OPENALEX_RECOMMENDATION_CACHE_LOCK:
        state = _services().OPENALEX_RECOMMENDATION_CACHE.get(cache_key) or {}
        cache_age = _services().time.monotonic() - float(state.get("at", 0) or 0)
        if cache_key and cache_age < _services().OPENALEX_RECOMMENDATION_TTL_SECONDS:
            rankings = state.get("rankings") or {}
            candidates = (
                rankings.get("external_recommendations")
                or rankings.get("external_candidates")
                or []
            )
            for item in candidates:
                item_external = _services().clean_text(
                    item.get("openalex_id") or item.get("external_id") or "", 500
                )
                matches_openalex = bool(
                    work_id and _services()._openalex_work_id(item_external) == work_id
                )
                requested_doi = _services().norm_doi(external_id)
                requested_arxiv = _services().norm_arxiv(external_id)
                matches_generic = bool(
                    not work_id
                    and (
                        item_external == _services().clean_text(external_id, 500)
                        or (
                            requested_doi
                            and _services().norm_doi(item.get("doi", ""))
                            == requested_doi
                        )
                        or (
                            requested_arxiv
                            and _services().norm_arxiv(item.get("arxiv_id", ""))
                            == requested_arxiv
                        )
                    )
                )
                if matches_openalex or matches_generic:
                    candidate = dict(item)
                    break
    if not candidate:
        raise LookupError("该推荐已过期或标识无效，请刷新推荐列表后重试")
    candidate["status"] = "to_read"
    provider_source = _services().clean_text(
        candidate.get("source") or ("openalex" if work_id else "semanticscholar"), 80
    )
    provider_external_id = _services().clean_text(
        candidate.get("openalex_id") or candidate.get("external_id") or external_id, 500
    )
    paper_id, created = _services().upsert_paper(
        candidate,
        source=provider_source,
        external_id=provider_external_id,
        raw={
            "recommendation_provider": candidate.get("recommendation_provider")
            or ("OpenAlex" if provider_source == "openalex" else "Semantic Scholar"),
            "recommendation_rank": candidate.get("recommendation_rank"),
        },
    )
    return {"id": paper_id, "created": created}


def research_memory(limit: int = 12) -> dict:
    limit = min(max(int(limit), 1), 50)
    # Recommendation and briefing endpoints often arrive together during the
    # home-page load. Build one bounded full projection and slice it per
    # caller so those requests never repeat the same full-library scan.
    token = _services().database_change_token()
    base = _services().cached_derived(
        f"research_memory:base:{token}",
        lambda: _services()._research_memory(50),
        ttl_seconds=30.0,
    )
    provider = _services()._openalex_recommendation_snapshot(
        base.get("_provider_seed_paper_ids", [])
    )
    external_recommendations = _services()._available_openalex_recommendations(
        provider.get("rankings", {}), limit=6
    )
    if provider.get("rankings"):
        provider_token = _services().clean_text(
            provider.get("updated_at") or provider.get("status"), 80
        )
        base = _services().cached_derived(
            f"research_memory:openalex:{token}:{provider_token}",
            lambda: _services()._research_memory(
                50, provider_rankings=provider["rankings"]
            ),
            ttl_seconds=30.0,
        )
    base = {key: value for key, value in base.items() if not key.startswith("_")}
    return {
        **base,
        "recommendations": list(base.get("recommendations", []))[:limit],
        "external_recommendations": external_recommendations,
        "recommendation_provider": {
            key: value for key, value in provider.items() if key != "rankings"
        },
    }


