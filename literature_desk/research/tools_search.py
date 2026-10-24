"""Agent search tool implementations."""

from __future__ import annotations

from ..services import app_services as _services

def _tool_build_search_plan(payload: dict) -> dict:
    goal = payload.get("goal") or {}
    query = _services().clean_text(
        goal.get("core_question") or goal.get("objective", ""), 1000
    )
    sources = [
        source
        for source in (goal.get("sources") or ["arxiv", "openalex", "crossref"])
        if source in _services().SOURCE_SEARCH
    ]
    result = _services().search_query_plan(
        query,
        sources,
        {
            "expand_concepts": True,
            "expand_relations": True,
            "search_depth": "balanced",
        },
    )
    result["query"] = result.get("original_query", query)
    return result


def _tool_search_sources(payload: dict) -> dict:
    goal = payload.get("goal") or {}
    query_plan = (
        payload.get("query_plan")
        if isinstance(payload.get("query_plan"), dict)
        else {}
    )
    query = _services().clean_text(
        query_plan.get("original_query")
        or query_plan.get("query")
        or goal.get("core_question")
        or goal.get("objective", ""),
        1000,
    )
    planned_sources = [
        item.get("id")
        for item in (query_plan.get("sources") or [])
        if isinstance(item, dict) and item.get("id") in _services().SOURCE_SEARCH
    ]
    sources = planned_sources or [
        source
        for source in (goal.get("sources") or ["arxiv", "openalex", "crossref"])
        if source in _services().SOURCE_SEARCH
    ]
    filters = (
        query_plan.get("filters")
        if isinstance(query_plan.get("filters"), dict)
        else {
            "search_depth": "balanced",
            "expand_concepts": True,
            "expand_relations": True,
        }
    )
    filters = {**filters, "auto_add": False}
    result = _services().run_query(
        query,
        sources,
        min(max(int(payload.get("limit", 30)), 5), 50),
        options=filters,
        plan_override=query_plan or None,
    )
    papers, quality = _services().filter_agent_candidate_quality(
        result.get("papers", []), query
    )
    summary = dict(result.get("summary") or {})
    summary["agent_quality"] = quality
    summary["filtered_count"] = quality.get("filtered_count", 0)
    if quality.get("gaps"):
        gaps = list(summary.get("gaps") or [])
        summary["gaps"] = list(dict.fromkeys(gaps + quality["gaps"]))
    return {
        "run_id": result["id"],
        "status": result["status"],
        "candidate_mode": True,
        "summary": summary,
        "candidate_ids": [paper["id"] for paper in papers],
        "papers": [_services()._agent_reduced_paper(paper) for paper in papers],
    }


def _tool_browse_web(payload: dict) -> dict:
    raw_urls = payload.get("urls") if isinstance(payload.get("urls"), list) else []
    if payload.get("url"):
        raw_urls = [payload.get("url"), *raw_urls]
    try:
        max_chars = min(max(int(payload.get("max_chars", 12000)), 500), 20000)
    except (TypeError, ValueError):
        max_chars = 12000
    return _services().browse_public_web(raw_urls, max_chars)


def _tool_fetch_web_page(payload: dict) -> dict:
    try:
        max_chars = min(max(int(payload.get("max_chars", 12000)), 500), 20000)
    except (TypeError, ValueError):
        max_chars = 12000
    return _services().fetch_deepseek_page(
        _services().clean_text(payload.get("url"), 2400), max_chars
    )


def _tool_search_local_library(payload: dict) -> dict:
    return _services().search_library(
        _services().clean_text(payload.get("query", ""), 500),
        min(max(int(payload.get("limit", 30)), 1), 100),
        0,
        "",
        "",
        {},
    )


def _tool_resolve_paper_identity(payload: dict) -> dict:
    papers = (
        payload.get("papers") if isinstance(payload.get("papers"), list) else []
    )
    if papers:
        return _services()._agent_resolve_paper_identities(
            papers, limit=min(max(int(payload.get("limit", 100)), 1), 100)
        )
    identity = _services().clean_text(
        payload.get("doi") or payload.get("arxiv_id") or payload.get("title", ""),
        1000,
    )
    with _services().DB_LOCK, _services().db_session() as db:
        doi = _services().norm_doi(payload.get("doi") or identity)
        arxiv_id = _services().norm_arxiv(payload.get("arxiv_id") or identity)
        title = _services().title_key(payload.get("title") or identity)
        row = db.execute(
            "SELECT * FROM papers WHERE (doi<>'' AND doi=?) OR (arxiv_id<>'' AND arxiv_id=?) OR (title_key<>'' AND title_key=?) LIMIT 1",
            (doi, arxiv_id, title),
        ).fetchone()
    return {
        "resolved": bool(row),
        "paper": _services().paper_dict(row) if row else None,
        "query": identity,
        "identity_status": (
            "doi"
            if row and doi and _services().norm_doi(row["doi"]) == doi
            else (
                "arxiv"
                if row
                and arxiv_id
                and _services().norm_arxiv(row["arxiv_id"]) == arxiv_id
                else ("title" if row else "unresolved")
            )
        ),
    }


def _tool_merge_paper_versions(payload: dict) -> dict:
    papers = [
        item for item in (payload.get("papers") or []) if isinstance(item, dict)
    ]
    identity_output = (
        payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
    )
    # The identity step can enrich a candidate with its local paper ID and
    # stable key.  Merge those annotations back without replacing newer
    # provider metadata from the search snapshot.
    resolved_items = (
        identity_output.get("papers")
        if isinstance(identity_output.get("papers"), list)
        else []
    )
    if resolved_items:
        papers = _services()._unique_agent_papers(papers, resolved_items, limit=100)
    groups = {}
    for paper in papers[:100]:
        key = _services().candidate_identity(paper)
        doi = _services().norm_doi(paper.get("doi", ""))
        arxiv_id = _services().norm_arxiv(paper.get("arxiv_id", ""))
        canonical = (
            ("doi:" + doi)
            if doi
            else (
                ("arxiv:" + arxiv_id)
                if arxiv_id
                else "title:" + _services().title_key(paper.get("title", ""))
            )
        )
        groups.setdefault(canonical, []).append(
            {
                **_services()._agent_reduced_paper(paper),
                "paper_key": key,
                "version_key": canonical,
            }
        )
    # DOI and arXiv identifiers can legitimately refer to different
    # editions of one work.  A conservative second pass joins only very
    # high title matches with compatible years and overlapping authors.
    canonical_keys = list(groups)
    for index, left_key in enumerate(canonical_keys):
        if left_key not in groups:
            continue
        left = groups[left_key][0]
        for right_key in canonical_keys[index + 1 :]:
            if right_key not in groups:
                continue
            right = groups[right_key][0]
            if (
                not left.get("title")
                or not right.get("title")
                or _services().title_similarity(left["title"], right["title"])
                < 0.95
            ):
                continue
            left_year, right_year = left.get("year"), right.get("year")
            if (
                left_year
                and right_year
                and abs(int(left_year) - int(right_year)) > 2
            ):
                continue
            left_authors = set(
                _services().relevance_tokens(left.get("authors", ""))
            )
            right_authors = set(
                _services().relevance_tokens(right.get("authors", ""))
            )
            if (
                left_authors
                and right_authors
                and not (left_authors & right_authors)
            ):
                continue
            version_key = (
                "work:"
                + _services()
                .hashlib.sha1(
                    (
                        _services().title_key(left["title"])
                        + "|"
                        + _services().title_key(right["title"])
                    ).encode("utf-8")
                )
                .hexdigest()[:20]
            )
            merged_versions = groups.pop(left_key) + groups.pop(right_key)
            for version in merged_versions:
                version["version_key"] = version_key
            groups[version_key] = merged_versions
            left = groups[version_key][0]
    now = _services().now_iso()
    persisted = []
    with _services().DB_LOCK, _services().db_session() as db:
        goal = payload.get("goal") or {}
        goal_id = _services().clean_text(goal.get("id", ""), 80)
        run_id = _services().clean_text(payload.get("run_id", ""), 80)
        for canonical, versions in groups.items():
            relation = (
                "same_work"
                if len(versions) == 1
                else "version_or_provider_duplicate"
            )
            for version in versions:
                paper_key = version["paper_key"]
                db.execute(
                    """INSERT INTO paper_version_links(canonical_key,paper_key,relation,confidence,metadata_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(canonical_key,paper_key) DO UPDATE SET relation=excluded.relation,confidence=excluded.confidence,metadata_json=excluded.metadata_json,last_seen_at=excluded.last_seen_at""",
                    (
                        canonical,
                        paper_key,
                        relation,
                        1.0 if canonical.startswith(("doi:", "arxiv:")) else 0.94,
                        _services().json.dumps(
                            {
                                "title": version.get("title", ""),
                                "year": version.get("year"),
                            },
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                    ),
                )
            persisted.append(
                {
                    "canonical_key": canonical,
                    "relation": relation,
                    "versions": versions,
                }
            )
        history = (
            _services()._record_goal_paper_history(db, goal_id, run_id, papers)
            if goal_id and run_id
            else {"new": [], "changed": [], "repeated": []}
        )
    return {
        "groups": persisted,
        "group_count": len(persisted),
        "duplicate_count": max(0, len(papers) - len(persisted)),
        "history": history,
    }


def _tool_rank_candidates(payload: dict) -> dict:
    papers = (
        payload.get("papers") if isinstance(payload.get("papers"), list) else []
    )
    goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else {}
    quality = None
    quality_query = _services().clean_text(
        goal.get("core_question") or goal.get("objective", ""), 1000
    )
    if quality_query:
        papers, quality = _services().filter_agent_candidate_quality(
            papers, quality_query
        )
    local_context = (
        payload.get("local_context")
        if isinstance(payload.get("local_context"), dict)
        else {}
    )
    local_rows = (
        local_context.get("results")
        if isinstance(local_context.get("results"), list)
        else []
    )
    ranked = sorted(
        papers,
        key=lambda item: (
            float(item.get("discovery_score") or item.get("relevance_score") or 0),
            int(item.get("year") or 0),
        ),
        reverse=True,
    )
    limit = min(max(int(payload.get("limit", 20)), 1), 50)
    candidates = []
    with _services().DB_LOCK, _services().db_session() as db:
        for item in ranked[:limit]:
            match = None
            if item.get("doi"):
                match = db.execute(
                    "SELECT id FROM papers WHERE doi=? LIMIT 1",
                    (_services().norm_doi(item["doi"]),),
                ).fetchone()
            if not match and item.get("arxiv_id"):
                match = db.execute(
                    "SELECT id FROM papers WHERE arxiv_id=? LIMIT 1",
                    (_services().norm_arxiv(item["arxiv_id"]),),
                ).fetchone()
            if not match:
                match = db.execute(
                    "SELECT id FROM papers WHERE title_key=? AND (year=? OR ? IS NULL) LIMIT 1",
                    (
                        _services().title_key(item.get("title", "")),
                        item.get("year"),
                        item.get("year"),
                    ),
                ).fetchone()
            reduced = _services()._agent_reduced_paper(item)
            reduced["local_paper_id"] = match["id"] if match else ""
            reduced["has_local_pdf"] = bool(
                match
                and db.execute(
                    "SELECT 1 FROM attachments WHERE paper_id=? LIMIT 1",
                    (match["id"],),
                ).fetchone()
            )
            reduced["local_document_pages"] = (
                int(
                    db.execute(
                        "SELECT COUNT(*) FROM document_pages WHERE paper_id=?",
                        (match["id"],),
                    ).fetchone()[0]
                )
                if match
                else 0
            )
            related = []
            for context in local_rows[:30]:
                if (
                    not isinstance(context, dict)
                    or not context.get("id")
                    or context.get("id") == item.get("id")
                ):
                    continue
                overlap = (
                    _services()
                    .text_overlap(
                        _services().paper_text(item),
                        _services().paper_text(context),
                    )
                    .get("score", 0)
                )
                if overlap >= 22:
                    related.append(
                        {
                            "paper_id": context.get("id"),
                            "title": context.get("title", ""),
                            "overlap": overlap,
                        }
                    )
            reduced["related_local_papers"] = sorted(
                related, key=lambda value: value["overlap"], reverse=True
            )[:3]
            candidates.append(reduced)
    focus = _services()._agent_focus_papers(
        candidates, limit=min(3, len(candidates))
    )
    focus_by_id = {item.get("id"): item for item in focus}
    for item in candidates:
        focused = focus_by_id.get(item.get("id"))
        if focused:
            item["focus_rank"] = focused.get("focus_rank")
            item["focus_reason"] = focused.get("focus_reason")
    output = {
        "total": len(ranked),
        "high_relevance": sum(
            item.get("relevance_label") == "高相关" for item in ranked
        ),
        "candidates": candidates,
        "excluded": max(0, len(ranked) - limit),
        "local_context_count": int(local_context.get("total") or len(local_rows)),
    }
    if quality is not None:
        output["quality"] = quality
        output["filtered_count"] = quality.get("filtered_count", 0)
    return output


def _tool_evaluate_coverage(payload: dict) -> dict:
    goal = payload.get("goal") or {}
    triage = payload.get("triage") or {}
    candidates = (
        triage.get("candidates")
        if isinstance(triage.get("candidates"), list)
        else []
    )
    labels = {
        str(item.get("topic") or "").strip()
        for item in candidates
        if item.get("topic")
    }
    abstracts = sum(bool(item.get("abstract")) for item in candidates)
    fulltext = sum(
        bool(item.get("local_document_pages") or item.get("has_local_pdf"))
        for item in candidates
    )
    gaps = []
    focus = goal.get("focus") or []
    if not candidates:
        gaps.append("没有找到候选论文")
    if len(candidates) < 5:
        gaps.append("候选数量不足以覆盖研究问题")
    if not abstracts:
        gaps.append("候选论文缺少摘要")
    if focus and not labels:
        gaps.append("研究目标关注维度尚未形成可分组主题")
    if goal.get("workflow") == "evidence_review" and not fulltext:
        gaps.append("尚无本地全文页级证据")
    return {
        "coverage": {
            "candidate_count": len(candidates),
            "abstract_count": abstracts,
            "fulltext_count": fulltext,
            "topic_count": len(labels),
            "focus_count": len(focus),
        },
        "gaps": gaps,
        "needs_expansion": bool(gaps and len(candidates) < 30),
        "stop_reason": (
            "覆盖达到当前预算" if not gaps else "存在可通过一次补搜改善的缺口"
        ),
    }


def _tool_expand_search(payload: dict) -> dict:
    goal = payload.get("goal") or {}
    coverage = payload.get("coverage") or (payload.get("previous") or {}).get(
        "evaluate_coverage", {}
    )
    if not coverage.get("needs_expansion"):
        return {
            "expanded": False,
            "reason": "当前覆盖已足够，无需补搜",
            "papers": [],
            "candidate_ids": [],
        }
    query = _services().clean_text(
        goal.get("core_question") or goal.get("objective", ""), 1000
    )
    sources = [
        source
        for source in (goal.get("sources") or _services().SOURCE_SEARCH.keys())
        if source in _services().SOURCE_SEARCH
    ]
    result = _services().run_query(
        query + " limitations failure analysis",
        sources,
        min(max(int(payload.get("limit", 15)), 5), 30),
        options={
            "search_depth": "focused",
            "expand_concepts": False,
            "expand_relations": False,
            "auto_add": False,
        },
    )
    papers = [
        _services()._agent_reduced_paper(item) for item in result.get("papers", [])
    ]
    with _services().DB_LOCK, _services().db_session() as db:
        history = (
            _services()._record_goal_paper_history(
                db,
                _services().clean_text(goal.get("id", ""), 80),
                _services().clean_text(payload.get("run_id", ""), 80),
                papers,
            )
            if goal.get("id")
            else {"new": [], "changed": [], "repeated": []}
        )
    return {
        "expanded": True,
        "run_id": result.get("id", ""),
        "papers": papers,
        "candidate_ids": [paper.get("id") for paper in papers],
        "history": history,
        "summary": result.get("summary", {}),
    }


__all__ = ['_tool_build_search_plan', '_tool_search_sources', '_tool_browse_web', '_tool_fetch_web_page', '_tool_search_local_library', '_tool_resolve_paper_identity', '_tool_merge_paper_versions', '_tool_rank_candidates', '_tool_evaluate_coverage', '_tool_expand_search']
