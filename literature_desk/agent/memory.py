"""Bounded, source-linked memory retrieval for autonomous research agents."""

from __future__ import annotations

from typing import Any

from ..services import app_services as _services


def retrieve_goal_memory(goal_id: str, query: str = "", *, limit: int = 12) -> dict[str, Any]:
    """Load compact durable context from prior goal runs and linked papers.

    Memory is deliberately assembled from existing authoritative records rather
    than copied into a second, potentially stale memory store.
    """
    cap = max(1, min(int(limit), 30))
    if not goal_id:
        return {"papers": [], "artifacts": [], "feedback": []}
    terms = [
        term
        for term in _services().re.findall(r"[\w\u4e00-\u9fff]{2,}", query.casefold())[:8]
    ]
    with _services().DB_LOCK, _services().db_session() as db:
        papers = db.execute(
            """SELECT rgp.paper_key,rgp.local_paper_id,rgp.first_seen_at,rgp.last_seen_at,
                      rgp.seen_count,rgp.user_decision,rgp.reading_status AS status,
                      COALESCE(NULLIF(rgp.title,''),p.title,'') AS title,
                      p.year,COALESCE(NULLIF(rgp.doi,''),p.doi,'') AS doi,
                      COALESCE(NULLIF(rgp.arxiv_id,''),p.arxiv_id,'') AS arxiv_id,
                      p.notes
               FROM research_goal_papers rgp LEFT JOIN papers p ON p.id=rgp.local_paper_id
               WHERE rgp.goal_id=? ORDER BY rgp.last_seen_at DESC LIMIT ?""",
            (goal_id, cap * 2),
        ).fetchall()
        artifacts = db.execute(
            """SELECT artifact_type,title,content_json,created_at FROM agent_artifacts
               WHERE goal_id=? ORDER BY created_at DESC LIMIT ?""",
            (goal_id, cap),
        ).fetchall()
    paper_items = []
    for row in papers:
        item = dict(row)
        haystack = " ".join(str(item.get(key) or "") for key in ("title", "notes", "doi", "arxiv_id")).casefold()
        score = sum(term in haystack for term in terms)
        item["memory_score"] = score
        paper_items.append(item)
    # Relevance wins; recency breaks ties.  Two stable sorts keep that order
    # explicit and avoid accidentally putting older records ahead of newer
    # ones when a query term matches equally well.
    paper_items.sort(key=lambda item: item.get("last_seen_at", ""), reverse=True)
    paper_items.sort(key=lambda item: item["memory_score"], reverse=True)
    artifact_items = []
    for row in artifacts:
        item = dict(row)
        item["content"] = _services()._bounded_agent_payload(
            _services()._json_value(item.pop("content_json", ""), {}),
            16_000,
        )
        artifact_items.append(item)
    return {
        "papers": paper_items[:cap],
        "artifacts": artifact_items[: min(cap, 8)],
        "feedback": [
            {
                "paper_id": item.get("local_paper_id") or item.get("paper_key", ""),
                "decision": item.get("user_decision", ""),
            }
            for item in paper_items if item.get("user_decision")
        ][:cap],
        "source": "research_goal_records",
    }


__all__ = ["retrieve_goal_memory"]
