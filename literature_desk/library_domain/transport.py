"""Public source HTTP transport and OpenAlex relation expansion."""

from __future__ import annotations

import sqlite3

from ..services import app_services as _services

def http_json(url: str, timeout=20) -> dict:
    """Fetch a public JSON endpoint with a small transient-error budget.

    Academic indexes are independent of the model request budget.  A single
    502 or socket reset should not make an otherwise healthy multi-source
    search report a permanent source failure, while 4xx responses remain
    visible quickly.  A 429 opens a short host-level circuit so parallel
    expansion lanes do not immediately repeat the same rate-limited request.
    """
    # Serialize only within this host.  The outer search still fans out across
    # independent sources, but a burst of variants cannot defeat OpenAlex's
    # rate limiter before the first response records a cooldown.
    with _services()._public_source_request_lock(url):
        transient_retries = 0
        timeout, retry_budget = _services()._public_source_request_policy(timeout)
        remaining = _services()._public_source_cooldown_remaining(url)
        if remaining:
            raise _services().PublicSourceCooldownError(
                f"{_services()._public_source_key(url)} 暂时限流，约 {max(1, round(remaining))} 秒后重试"
            )
        request_url = url
        request_headers = {
            "User-Agent": _services().USER_AGENT,
            "Accept": "application/json",
        }
        host = _services()._public_source_key(url)
        if host == "api.openalex.org":
            api_key = _services().os.environ.get("LIT_OPENALEX_API_KEY", "").strip()
            existing_keys = (
                _services()
                .parse_qs(_services().urlparse(url).query, keep_blank_values=True)
                .get("api_key", [])
            )
            if api_key and not any(existing_keys):
                request_url += (
                    "&" if "?" in request_url else "?"
                ) + _services().urlencode({"api_key": api_key})
        elif host == "api.semanticscholar.org":
            api_key = (
                _services().os.environ.get("LIT_SEMANTIC_SCHOLAR_API_KEY", "").strip()
            )
            if api_key:
                request_headers["x-api-key"] = api_key
        for _attempt in range(retry_budget + 1):
            req = _services().Request(request_url, headers=request_headers)
            try:
                with _services().urlopen(req, timeout=timeout) as response:
                    return _services().json.loads(
                        response.read().decode("utf-8", "replace")
                    )
            except _services().HTTPError as exc:
                if exc.code == 429:
                    cooldown = _services()._public_retry_after(exc.headers)
                    _services()._cooldown_public_source(url, cooldown)
                    raise
                if exc.code in {401, 403}:
                    # Authentication/robots responses are not fixed by retrying
                    # every query variant. Keep the source visible as unavailable
                    # while allowing the other scholarly indexes to proceed.
                    _services()._cooldown_public_source(
                        url, max(60.0, _services().PUBLIC_SOURCE_COOLDOWN_SECONDS * 4)
                    )
                    raise
                if (
                    exc.code in {408, 425, 500, 502, 503, 504, 524}
                    and transient_retries < retry_budget
                ):
                    _services().time.sleep(
                        min(
                            _services()._model_retry_delay(
                                transient_retries, exc.headers
                            ),
                            2.0,
                        )
                    )
                    transient_retries += 1
                    continue
                raise
            except (_services().URLError, TimeoutError, ConnectionError, OSError):
                if transient_retries >= retry_budget:
                    raise
                _services().time.sleep(
                    min(_services()._model_retry_delay(transient_retries), 2.0)
                )
                transient_retries += 1
            except (
                _services().json.JSONDecodeError,
                UnicodeDecodeError,
                ValueError,
            ) as exc:
                # Empty/HTML responses are common during provider maintenance.
                # Retry once, then preserve a source-level diagnostic instead of
                # letting a parser traceback escape from the worker.
                if transient_retries < retry_budget:
                    _services().time.sleep(
                        min(_services()._model_retry_delay(transient_retries), 1.0)
                    )
                    transient_retries += 1
                    continue
                _services()._cooldown_public_source(
                    url, _services().PUBLIC_SOURCE_COOLDOWN_SECONDS
                )
                raise ValueError(
                    f"{_services()._public_source_key(url)} 返回了非 JSON 内容"
                ) from exc
        raise RuntimeError(f"{_services()._public_source_key(url)} 请求重试次数已用尽")


def http_bytes(url: str, timeout: int | None = None, accept: str = "*/*") -> bytes:
    """Fetch bounded public-source bytes with the same cooldown policy."""
    if timeout is None:
        timeout = _services().PUBLIC_SOURCE_TIMEOUT
    with _services()._public_source_request_lock(url):
        timeout, retry_budget = _services()._public_source_request_policy(timeout)
        remaining = _services()._public_source_cooldown_remaining(url)
        if remaining:
            raise _services().PublicSourceCooldownError(
                f"{_services()._public_source_key(url)} 暂时限流，约 {max(1, round(remaining))} 秒后重试"
            )
        transient_retries = 0
        for _attempt in range(retry_budget + 1):
            req = _services().Request(
                url, headers={"User-Agent": _services().USER_AGENT, "Accept": accept}
            )
            try:
                with _services().urlopen(req, timeout=timeout) as response:
                    return response.read()
            except _services().HTTPError as exc:
                if exc.code == 429:
                    _services()._cooldown_public_source(
                        url, _services()._public_retry_after(exc.headers)
                    )
                    raise
                if exc.code in {401, 403}:
                    _services()._cooldown_public_source(
                        url, max(60.0, _services().PUBLIC_SOURCE_COOLDOWN_SECONDS * 4)
                    )
                    raise
                if (
                    exc.code in {408, 425, 500, 502, 503, 504, 524}
                    and transient_retries < retry_budget
                ):
                    _services().time.sleep(
                        min(
                            _services()._model_retry_delay(
                                transient_retries, exc.headers
                            ),
                            2.0,
                        )
                    )
                    transient_retries += 1
                    continue
                raise
            except (_services().URLError, TimeoutError, ConnectionError, OSError):
                if transient_retries >= retry_budget:
                    raise
                _services().time.sleep(
                    min(_services()._model_retry_delay(transient_retries), 2.0)
                )
                transient_retries += 1
        raise RuntimeError(f"{_services()._public_source_key(url)} 请求重试次数已用尽")


def _openalex_work_reference(
    paper: dict, *, local_paper_id: str = ""
) -> tuple[str, str]:
    """Return a safe OpenAlex lookup URL and the associated library paper ID."""
    doi = _services().norm_doi(paper.get("doi", ""))
    openalex_id = _services().clean_text(
        paper.get("openalex_id") or paper.get("external_id") or "", 300
    )
    if openalex_id.startswith("https://openalex.org/"):
        openalex_id = openalex_id.rstrip("/").split("/")[-1]
    if openalex_id and _services().re.fullmatch(r"W\d+", openalex_id, _services().re.I):
        return (
            "https://api.openalex.org/works/" + _services().quote(openalex_id, safe=""),
            local_paper_id,
        )
    if doi:
        return (
            "https://api.openalex.org/works/https://doi.org/"
            + _services().quote(doi, safe=""),
            local_paper_id,
        )
    raise ValueError("该论文没有 DOI 或 OpenAlex 标识，暂时无法扩展引用关系")


def _openalex_relation_candidate(item: dict, relation: str) -> dict:
    inverted = item.get("abstract_inverted_index") or {}
    words = []
    for word, positions in inverted.items():
        for position in positions or []:
            try:
                words.append((int(position), word))
            except (TypeError, ValueError):
                continue
    primary = item.get("primary_location") or {}
    return {
        "relation": relation,
        "openalex_id": item.get("id", ""),
        "title": _services().clean_text(item.get("title", ""), 1000),
        "authors": ", ".join(
            (entry.get("author") or {}).get("display_name", "")
            for entry in (item.get("authorships") or [])[:12]
        ),
        "year": item.get("publication_year"),
        "abstract": " ".join(word for _, word in sorted(words)),
        "doi": _services().norm_doi(item.get("doi", "")),
        "venue": ((primary.get("source") or {}).get("display_name", "")),
        "url": item.get("doi") or item.get("id", ""),
        "pdf_url": (primary.get("pdf") or {}).get("url", "") or "",
        "source": "openalex",
        "external_id": item.get("id", "") or _services().norm_doi(item.get("doi", "")),
        "type": _services().clean_text(item.get("type", "journal-article"), 100),
        "citation_count": int(item.get("cited_by_count") or 0),
    }


def _expand_openalex_relation_work(
    work_url: str,
    limit: int = 24,
    *,
    persist: bool = False,
    paper_id: str = "",
    request_timeout: float = 15,
    include_cited_by: bool = True,
) -> dict:
    """Fetch a bounded citation neighborhood using one batched work-details request.

    ``include_cited_by`` and ``request_timeout`` are explicit because Agent
    runs must remain responsive.  The forward citation endpoint is useful but
    optional; references and related works from the primary work response are
    enough to keep a bounded discovery lane alive when that endpoint is slow.
    Interactive expansion keeps the complete default behavior.
    """
    limit = min(max(int(limit), 1), 60)
    try:
        timeout = max(2.0, min(float(request_timeout), 30.0))
    except (TypeError, ValueError):
        timeout = 15.0
    work = _services().http_json(work_url, timeout=timeout)
    if not work.get("id") and work.get("results"):
        work = work["results"][0]
    if not work.get("id"):
        raise ValueError("OpenAlex 没有解析到这篇论文")
    work_id = str(work.get("id", "")).rstrip("/").split("/")[-1]
    relation_by_id = {}
    for relation, ids in (
        ("引用", work.get("referenced_works") or []),
        ("相似", work.get("related_works") or []),
    ):
        for target in ids:
            key = str(target).rstrip("/").split("/")[-1]
            if key and key != work_id:
                relation_by_id.setdefault(key, relation)
    cited_url = work.get("cited_by_api_url", "")
    if include_cited_by and cited_url:
        try:
            cited = _services().http_json(
                cited_url
                + ("&" if "?" in cited_url else "?")
                + _services().urlencode(
                    {"per-page": min(limit, 25), "sort": "publication_date:desc"}
                ),
                timeout=timeout,
            )
            for item in cited.get("results", []):
                key = str(item.get("id", "")).rstrip("/").split("/")[-1]
                if key and key != work_id:
                    relation_by_id.setdefault(key, "被引用")
        except (
            _services().HTTPError,
            _services().URLError,
            TimeoutError,
            OSError,
            ValueError,
        ):
            # Citation-forward data is optional; references and related works remain useful.
            pass

    selected_ids = list(relation_by_id)[:limit]
    records_by_id = {}
    if selected_ids:
        batch_url = "https://api.openalex.org/works?" + _services().urlencode(
            {
                "filter": "ids.openalex:" + "|".join(selected_ids),
                "per-page": len(selected_ids),
            }
        )
        try:
            batch = _services().http_json(batch_url, timeout=timeout)
            records_by_id = {
                str(item.get("id", "")).rstrip("/").split("/")[-1]: item
                for item in batch.get("results", [])
                if item.get("id")
            }
            if not records_by_id:
                raise ValueError("OpenAlex 批量接口返回空结果")
        except (
            _services().HTTPError,
            _services().URLError,
            TimeoutError,
            OSError,
            ValueError,
        ):
            # Keep a bounded compatibility path for API proxies that reject OR filters.
            def fetch_one(key):
                try:
                    return key, _services().http_json(
                        "https://api.openalex.org/works/"
                        + _services().quote(key, safe=""),
                        timeout=max(2.0, min(timeout, 8.0)),
                    )
                except (
                    _services().HTTPError,
                    _services().URLError,
                    TimeoutError,
                    OSError,
                    ValueError,
                ):
                    return key, None

            with _services().ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="openalex-details"
            ) as pool:
                for key, item in pool.map(fetch_one, selected_ids):
                    if item:
                        records_by_id[key] = item

    candidates = []
    for key in selected_ids:
        item = records_by_id.get(key)
        if not item or not _services().clean_text(item.get("title", ""), 1000):
            continue
        candidates.append(
            _services()._openalex_relation_candidate(item, relation_by_id[key])
        )
    created = duplicates = 0
    if persist:
        for record in candidates:
            _, is_new = _services().upsert_paper(
                record,
                source="openalex",
                external_id=record.get("external_id", ""),
                raw={"relation": record["relation"], "seed_paper_id": paper_id},
            )
            created += int(is_new)
            duplicates += int(not is_new)
        with _services().DB_LOCK, _services().db_session() as db:
            _services().record_audit(
                db,
                "discovery.expanded",
                "paper",
                paper_id,
                {
                    "candidates": len(candidates),
                    "created": created,
                    "duplicates": duplicates,
                },
            )
    return {
        "paper_id": paper_id,
        "provider": "OpenAlex",
        "candidates": candidates,
        "created": created,
        "duplicates": duplicates,
        "count": len(candidates),
        "persisted": bool(persist),
        "expanded_at": _services().now_iso(),
    }


def sync_openalex_references(paper_id: str) -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        paper = db.execute(
            "SELECT id,title,doi,arxiv_id FROM papers WHERE id=?", (paper_id,)
        ).fetchone()
    if not paper:
        raise ValueError("论文不存在")
    if not paper["doi"]:
        raise ValueError("该论文还没有 DOI；目前 OpenAlex 引用扩展需要 DOI")
    work_url = "https://api.openalex.org/works/https://doi.org/" + _services().quote(
        paper["doi"], safe=""
    )
    work = _services().http_json(work_url, timeout=25)
    referenced = work.get("referenced_works") or []
    observed = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        for target in referenced:
            external_id = _services().clean_text(target, 300)
            if not external_id:
                continue
            db.execute(
                """INSERT INTO citation_edges(source_paper_id,target_external_id,provider,discovered_at,last_seen_at,raw_json)
                VALUES(?,?,?,?,?,?) ON CONFLICT(source_paper_id,target_external_id,provider) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                (paper_id, external_id, "openalex", observed, observed, "{}"),
            )
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT target_external_id,provider,discovered_at,last_seen_at FROM citation_edges WHERE source_paper_id=? ORDER BY target_external_id",
                (paper_id,),
            )
        ]
    return {
        "paper_id": paper_id,
        "provider": "OpenAlex",
        "count": len(rows),
        "references": rows,
        "synced_at": observed,
    }


def expand_openalex_relations(
    paper_id: str,
    limit: int = 24,
    *,
    persist: bool = True,
    request_timeout: float = 15,
    include_cited_by: bool = True,
) -> dict:
    """Discover backward, forward and semantically related works from OpenAlex.

    This is an explicit user action because citation expansion can make several
    network requests.  New works enter the normal inbox and are still subject
    to the same conservative deduplication rules as keyword search.
    """
    limit = min(max(int(limit), 1), 60)
    with _services().DB_LOCK, _services().db_session() as db:
        paper = db.execute(
            "SELECT id,title,doi,arxiv_id FROM papers WHERE id=?", (paper_id,)
        ).fetchone()
        source_id = db.execute(
            "SELECT external_id FROM source_records WHERE paper_id=? AND source='openalex' AND external_id<>'' ORDER BY fetched_at DESC LIMIT 1",
            (paper_id,),
        ).fetchone()
    if not paper:
        raise ValueError("论文不存在")
    if paper["doi"]:
        work_url = (
            "https://api.openalex.org/works/https://doi.org/"
            + _services().quote(paper["doi"], safe="")
        )
    elif source_id and str(source_id[0]).strip():
        work_url = "https://api.openalex.org/works/" + _services().quote(
            str(source_id[0]).rstrip("/").split("/")[-1], safe=""
        )
    else:
        raise ValueError("该论文没有 DOI 或 OpenAlex 标识，暂时无法扩展引用关系")
    return _services()._expand_openalex_relation_work(
        work_url,
        limit=limit,
        persist=persist,
        paper_id=paper_id,
        request_timeout=request_timeout,
        include_cited_by=include_cited_by,
    )


__all__ = [
    "norm_doi",
    "norm_arxiv",
    "title_key",
    "relevance_tokens",
    "expanded_relevance_terms",
    "search_plan_terms",
    "extract_search_facets",
    "_bridge_query_terms",
    "_bridge_non_latin_terms",
    "search_query_variants",
    "candidate_identity",
    "candidate_identity_variants",
    "record_discovery_search_memory",
    "discovery_intent_key",
    "_json_object",
    "_discovery_feedback_by_key",
    "_discovery_feedback_examples",
    "title_similarity",
    "merge_search_records",
    "relevance_label",
    "relevance_token_weight",
    "score_relevance",
    "_model_web_has_object_task_evidence",
    "filter_model_web_quality",
    "filter_agent_candidate_quality",
    "clean_text",
    "compact_search_raw",
    "compact_candidate_record",
    "normalize_record",
    "paper_pdf_candidates",
    "openalex_public_pdf_candidates",
    "europepmc_public_pdf_candidates",
    "semantic_scholar_public_pdf_candidates",
    "download_public_pdf",
    "paper_text",
    "text_overlap",
    "connection_tier",
    "_openalex_work_id",
    "_openalex_seed_ids",
    "_semantic_scholar_seed_ids",
    "_semantic_recommendation_record",
    "_load_semantic_scholar_recommendations",
    "_load_openalex_recommendations",
    "_merge_recommendation_provider_results",
    "_refresh_openalex_recommendations",
    "_openalex_recommendation_snapshot",
    "_available_openalex_recommendations",
    "save_openalex_recommendation",
    "research_memory",
    "_research_memory",
    "paper_connections",
    "paper_dict",
    "_hydrate_retrieval_metadata",
    "paper_detail",
    "refresh_search_content",
    "rebuild_search_index",
    "search_library",
    "maintenance_status",
    "get_backup_policy",
    "save_backup_policy",
    "prune_backup_archives",
    "create_backup_archive",
    "scheduled_backup_tick",
    "paper_history",
    "reading_card_dict",
    "record_paper_revision",
    "save_reading_card",
    "batch_update_papers",
    "delete_papers",
    "attach_source",
    "find_existing_paper",
    "upsert_paper",
    "http_json",
    "http_bytes",
    "_openalex_work_reference",
    "_openalex_relation_candidate",
    "_expand_openalex_relation_work",
    "sync_openalex_references",
    "expand_openalex_relations",
]
