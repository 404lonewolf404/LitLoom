"""Normalize model search results into the scholarly candidate contract."""

from __future__ import annotations

from ..services import app_services as _services

class ModelWebSearchUnsupported(ValueError):
    """The configured model endpoint does not expose a web-search tool."""


def _model_search_url(value: object) -> str:
    """Return a usable source URL without accepting model-invented schemes."""
    url = _services().clean_text(value, 1200).strip()
    parsed = _services().urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _model_search_year(value: object) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1800 <= year <= 2200 else None


def normalize_model_search_results(
    raw: object, query: str, provider: str = "", model: str = ""
) -> dict:
    """Normalize provider/tool output into the same candidate contract as connectors.

    A model is allowed to return partial metadata, but a result without a
    title and a verifiable HTTP(S) source is discarded. This prevents a relay
    from turning a prose answer into fake papers or unusable candidates.
    """
    if not isinstance(raw, dict):
        raw = {}
    values = (
        raw.get("results")
        or raw.get("papers")
        or raw.get("items")
        or raw.get("references")
        or []
    )
    if isinstance(values, dict):
        values = list(values.values())
    if not isinstance(values, list):
        values = []
    results, seen = [], set()
    unrelated_filtered = 0
    for item in values:
        if not isinstance(item, dict):
            continue
        title = _services().clean_text(
            item.get("title") or item.get("paper_title") or item.get("name"), 600
        )
        url = _services()._model_search_url(
            item.get("url") or item.get("source_url") or item.get("link")
        )
        citations = (
            item.get("source_citations")
            or item.get("citations")
            or item.get("sources")
            or []
        )
        if isinstance(citations, str):
            citations = [citations]
        citations = (
            [
                _services()._model_search_url(value)
                for value in citations
                if _services()._model_search_url(value)
            ]
            if isinstance(citations, list)
            else []
        )
        if not url and citations:
            url = citations[0]
        doi = _services().norm_doi(item.get("doi") or "")
        arxiv_id = _services().norm_arxiv(
            item.get("arxiv_id") or item.get("arxiv") or ""
        )
        identity = (
            doi
            or arxiv_id
            or _services().clean_text(item.get("external_id") or "", 300)
            or _services().title_key(title)
        )
        if not title or not url or not identity or identity in seen:
            continue
        seen.add(identity)
        authors = item.get("authors") or item.get("author") or ""
        if isinstance(authors, list):
            authors = ", ".join(
                _services().clean_text(
                    author.get("name") if isinstance(author, dict) else author, 160
                )
                for author in authors[:20]
            )
        label = (
            _services()
            .clean_text(
                item.get("relation")
                or item.get("relevance_label")
                or item.get("category")
                or "uncertain",
                40,
            )
            .lower()
        )
        label = {
            "direct": "direct",
            "relevant": "direct",
            "high": "direct",
            "adjacent": "adjacent",
            "nearby": "adjacent",
            "unrelated": "unrelated",
        }.get(label, "uncertain")
        if label == "unrelated":
            unrelated_filtered += 1
            continue
        results.append(
            {
                "title": title,
                "authors": _services().clean_text(authors, 1200),
                "year": _services()._model_search_year(
                    item.get("year") or item.get("publication_year")
                ),
                "abstract": _services().clean_text(
                    item.get("abstract")
                    or item.get("summary")
                    or item.get("description"),
                    8000,
                ),
                "doi": doi,
                "arxiv_id": arxiv_id,
                "venue": _services().clean_text(
                    item.get("venue") or item.get("journal") or item.get("conference"),
                    500,
                ),
                "url": url,
                "pdf_url": _services()._model_search_url(
                    item.get("pdf_url") or item.get("pdf")
                ),
                "source": "model_web",
                "external_id": _services().clean_text(
                    item.get("external_id") or doi or arxiv_id or identity, 500
                ),
                "open_access": bool(item.get("open_access")),
                "citation_count": (
                    max(0, int(item.get("citation_count") or 0))
                    if str(item.get("citation_count") or "").strip().isdigit()
                    else 0
                ),
                "relevance_label": label,
                "relevance_reason": _services().clean_text(
                    item.get("relevance_reason") or item.get("reason") or "", 500
                ),
                "source_citations": citations,
                "source_attested": bool(citations or url),
                "verified": bool(item.get("verification_status") == "verified"),
                "verification_status": _services().clean_text(
                    item.get("verification_status")
                    or ("verified" if item.get("academic_source") else "unverified"),
                    40,
                ),
                "verification_sources": [
                    _services().clean_text(value, 80)
                    for value in (item.get("verification_sources") or [])
                    if _services().clean_text(value, 80)
                ],
                "discovery_only": bool(item.get("discovery_only")),
                "tavily_score": item.get("tavily_score", 0),
                "academic_source": _services().clean_text(
                    item.get("academic_source"), 80
                ),
                "model_provider": provider,
                "model": model,
                # Preserve the bounded local-fill audit marker.  These are real
                # connector candidates which matched the requested object/task
                # but were omitted from the model's short final ranking; the
                # later quality gate must keep them as explicit review items.
                "selection_backfilled": bool(
                    item.get("_selection_backfilled")
                    or item.get("selection_backfilled")
                ),
                # Durable history rows carry the result of the earlier quality
                # gate.  Preserve it through model normalization so a repeat
                # search does not discard accepted history during the next pass.
                "history_quality_accepted": bool(item.get("history_quality_accepted")),
            }
        )
    gaps = raw.get("gaps") or raw.get("limitations") or []
    next_queries = raw.get("next_queries") or raw.get("follow_up_queries") or []
    return {
        "results": results,
        "searches": (
            raw.get("searches") if isinstance(raw.get("searches"), list) else []
        ),
        "citations": (
            raw.get("citations") if isinstance(raw.get("citations"), list) else []
        ),
        "gaps": (
            [
                _services().clean_text(value, 500)
                for value in gaps
                if _services().clean_text(value, 500)
            ][:12]
            if isinstance(gaps, list)
            else []
        ),
        "next_queries": (
            [
                _services().clean_text(value, 500)
                for value in next_queries
                if _services().clean_text(value, 500)
            ][:12]
            if isinstance(next_queries, list)
            else []
        ),
        "provider": provider,
        "model": model,
        "warnings": [],
        "unrelated_results_filtered": unrelated_filtered,
        "ok": bool(results),
    }


__all__ = ['ModelWebSearchUnsupported', '_model_search_url', '_model_search_year', 'normalize_model_search_results']
