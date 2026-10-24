"""Durable research memory assembled from library and discovery history."""

from __future__ import annotations

import sqlite3

from ..services import app_services as _services

def _research_memory(limit: int = 12, provider_rankings: dict | None = None) -> dict:
    """Build a deterministic research profile and non-repeating queue.

    The profile is intentionally local and inspectable. It can later be
    replaced by embeddings without changing the API contract or losing the
    feedback history accumulated here.
    """
    limit = min(max(int(limit), 1), 50)
    with _services().DB_LOCK, _services().db_session() as db:
        papers = [
            _services().paper_dict(row)
            for row in db.execute("SELECT * FROM papers ORDER BY updated_at DESC")
        ]
        queries = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM saved_queries ORDER BY active DESC,updated_at DESC"
            )
        ]
        matches = [dict(row) for row in db.execute("SELECT * FROM query_matches")]
        feedback = [dict(row) for row in db.execute("SELECT * FROM learning_feedback")]
    for query in queries:
        query["sources"] = _services().json.loads(query.pop("sources_json", "[]"))
    by_id = {paper["id"]: paper for paper in papers}
    match_by_paper = {}
    for item in matches:
        match_by_paper.setdefault(item["paper_id"], []).append(item)
    feedback_by_paper = {
        item["paper_id"]: item for item in feedback if item.get("scope") == "global"
    }

    # Recommendation ranking does not need the full abstract payload. Keep a
    # bounded local text window and compute its expanded terms exactly once;
    # the previous implementation re-tokenized the same paper for every
    # anchor/query comparison, which became noticeable on larger libraries.
    def ranking_text(record):
        tags = record.get("tags") if isinstance(record, dict) else []
        if isinstance(tags, str):
            tags = [tags]
        abstract = str(record.get("abstract") or "")[:900]
        return (
            " ".join(
                str(record.get(key) or "") for key in ("title", "authors", "topic")
            )
            + " "
            + " ".join(tags or [])
            + " "
            + abstract
        )

    terms_by_paper = {
        paper["id"]: _services().expanded_relevance_terms(ranking_text(paper))
        for paper in papers
    }

    def overlap_terms(left_terms, right_terms):
        if not left_terms or not right_terms:
            return {"score": 0, "hits": []}
        hits = sorted(left_terms & right_terms)
        coverage = len(hits) / max(1, min(len(left_terms), len(right_terms)))
        jaccard = len(hits) / max(1, len(left_terms | right_terms))
        return {
            "score": min(100, round((coverage * 0.7 + jaccard * 0.3) * 100)),
            "hits": hits[:8],
        }

    profile_queries = [query for query in queries if query.get("active")] or queries
    query_match_cache = {}

    def query_match(candidate):
        cached = query_match_cache.get(candidate["id"])
        if cached is not None:
            return cached
        best = {"score": 0, "query": "", "reasons": []}
        for query in profile_queries:
            result = _services().score_relevance(
                candidate, query.get("query", ""), [], []
            )
            if result["score"] > best["score"]:
                best = {
                    "score": result["score"],
                    "query": query.get("name") or query.get("query", ""),
                    "reasons": result.get("reasons", []),
                }
        query_match_cache[candidate["id"]] = best
        return best

    anchors = [
        paper
        for paper in papers
        if paper.get("status") == "read"
        or paper.get("is_favorite")
        or any(
            item.get("user_decision") == "relevant"
            for item in match_by_paper.get(paper["id"], [])
        )
    ]
    # A favorite from another research direction must not steer a topic queue.
    # Keep global reading anchors only when there is no saved research query.
    if profile_queries:
        anchors = [paper for paper in anchors if query_match(paper)["score"] >= 35]
    profile_counter = {}
    for paper in anchors:
        for term in terms_by_paper.get(paper["id"], set()):
            profile_counter[term] = profile_counter.get(term, 0) + 1
    top_terms = [
        term
        for term, _ in sorted(
            profile_counter.items(), key=lambda pair: (-pair[1], pair[0])
        )[:12]
    ]
    topic_counts = {}
    for paper in anchors:
        topic = paper.get("topic") or "未分类"
        topic_counts[topic] = topic_counts.get(topic, 0) + 1

    def anchor_match(candidate):
        best = {"score": 0, "title": "", "hits": []}
        for anchor in anchors:
            result = overlap_terms(
                terms_by_paper.get(candidate["id"], set()),
                terms_by_paper.get(anchor["id"], set()),
            )
            if result["score"] > best["score"]:
                best = {**result, "title": anchor.get("title", "")}
        return best

    provider_scores = {}
    if provider_rankings:
        by_openalex = provider_rankings.get("by_openalex") or {}
        by_doi = provider_rankings.get("by_doi") or {}
        if by_openalex:
            work_ids = list(by_openalex)[:100]
            external_forms = list(
                dict.fromkeys(
                    form
                    for work_id in work_ids
                    for form in (work_id, "https://openalex.org/" + work_id)
                )
            )
            placeholders = ",".join("?" for _ in external_forms)
            with _services().DB_LOCK, _services().db_session() as db:
                source_rows = db.execute(
                    f"SELECT paper_id,external_id FROM source_records WHERE source='openalex' AND external_id IN ({placeholders})",
                    external_forms,
                ).fetchall()
            for row in source_rows:
                work_id = _services()._openalex_work_id(row["external_id"])
                signal = by_openalex.get(work_id)
                if signal:
                    current = provider_scores.get(row["paper_id"])
                    if not current or signal.get("score", 0) > current.get("score", 0):
                        provider_scores[row["paper_id"]] = signal
        for paper in papers:
            doi = _services().norm_doi(paper.get("doi", ""))
            signal = by_doi.get(doi) if doi else None
            if signal:
                current = provider_scores.get(paper["id"])
                if not current or signal.get("score", 0) > current.get("score", 0):
                    provider_scores[paper["id"]] = signal

    candidates = []
    for paper in papers:
        if paper["status"] not in {"inbox", "to_read", "later", "watching"}:
            continue
        global_feedback = feedback_by_paper.get(paper["id"], {})
        if global_feedback.get("decision") == "irrelevant":
            continue
        qmatch = query_match(paper)
        amatch = anchor_match(paper)
        seen = sum(
            int(item.get("seen_count") or 0)
            for item in match_by_paper.get(paper["id"], [])
        )
        ignored = sum(
            item.get("user_decision") in {"irrelevant", "ignored"}
            for item in match_by_paper.get(paper["id"], [])
        )
        score = (
            round(qmatch["score"] * 0.9 + amatch["score"] * 0.1)
            if profile_queries
            else amatch["score"]
        )
        if profile_queries:
            score = min(score, qmatch["score"] + 8)
        provider_signal = provider_scores.get(paper["id"])
        if provider_signal:
            score = round(score * 0.65 + int(provider_signal.get("score", 0)) * 0.35)
        if paper.get("topic") and paper.get("topic") in topic_counts:
            score += 8
        if seen > 1:
            score -= min(20, (seen - 1) * 5)
        score -= min(30, ignored * 15)
        if not anchors and not queries:
            score = 0
        reasons = []
        if qmatch["score"]:
            reasons.append(f"命中主题：{qmatch['query']}")
            if qmatch["reasons"]:
                reasons.append(qmatch["reasons"][0])
        elif profile_queries and qmatch["reasons"]:
            reasons.append(qmatch["reasons"][0])
        if amatch["score"] >= 35:
            reasons.append(f"接近已读论文：{amatch['title'][:36]}")
        if provider_signal:
            provider_name = _services().clean_text(
                provider_signal.get("provider") or "OpenAlex", 80
            )
            if provider_name.casefold() == "openalex":
                reasons.append(
                    f"OpenAlex 相关工作推荐（相似于 {int(provider_signal.get('seed_count', 1))} 篇阅读锚点）"
                )
            else:
                reasons.append(
                    f"{provider_name} 推荐（相似于 {int(provider_signal.get('seed_count', 1))} 篇阅读锚点）"
                )
        if paper.get("topic") and paper.get("topic") in topic_counts:
            reasons.append(f"属于你的研究主题：{paper['topic']}")
        if not seen:
            reasons.append("尚未在自动主题中重复出现")
        if not reasons:
            if not anchors and not queries:
                reasons.append("尚未形成研究画像，按最新入库保留")
            else:
                reasons.append("作为跨领域线索保留，建议先扫读摘要")
        item = {
            **paper,
            "recommendation_score": max(0, min(100, score)),
            "recommendation_reasons": reasons,
            "seen_count": seen,
        }
        candidates.append(item)
    # Keep fresh arrivals ahead when scores tie, then apply the learned score.
    candidates.sort(
        key=lambda item: (item.get("updated_at", ""), item.get("title", "")),
        reverse=True,
    )
    candidates.sort(key=lambda item: item["recommendation_score"], reverse=True)
    selected = []
    topic_slots = {}
    for item in candidates:
        topic = item.get("topic") or "未分类"
        if topic_slots.get(topic, 0) >= 3:
            continue
        selected.append(item)
        topic_slots[topic] = topic_slots.get(topic, 0) + 1
        if len(selected) >= limit:
            break
    return {
        "generated_at": _services().now_iso(),
        "profile": {
            "anchor_count": len(anchors),
            "top_terms": top_terms,
            "top_topics": sorted(
                topic_counts.items(), key=lambda pair: (-pair[1], pair[0])
            )[:8],
            "feedback_count": len(feedback),
        },
        "recommendations": selected,
        "_provider_seed_paper_ids": [paper["id"] for paper in anchors[:3]],
    }


__all__ = ['_research_memory']
