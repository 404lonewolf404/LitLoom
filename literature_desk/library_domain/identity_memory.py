"""Canonical paper identity and durable discovery history helpers."""

from __future__ import annotations

from ..services import app_services as _services

def candidate_identity(record: dict) -> str:
    """Build a stable per-source identity for merging variant hits."""
    doi = _services().norm_doi(record.get("doi", ""))
    if doi:
        return "doi:" + doi
    arxiv = _services().norm_arxiv(record.get("arxiv_id", ""))
    if arxiv:
        return "arxiv:" + arxiv
    title = _services().title_key(record.get("title", ""))
    year = record.get("year") or ""
    if title:
        return f"title:{title}:{year}"
    external = _services().clean_text(record.get("external_id", ""), 300).casefold()
    return (
        "external:" + external
        if external
        else "record:"
        + _services()
        .hashlib.sha1(
            _services()
            .json.dumps(record, sort_keys=True, ensure_ascii=False)
            .encode("utf-8")
        )
        .hexdigest()
    )


def candidate_identity_variants(record: dict) -> set[str]:
    """Return stable identity aliases to catch DOI/arXiv/title crosswalks."""
    keys = {_services().candidate_identity(record)}
    doi = _services().norm_doi(record.get("doi", ""))
    arxiv = _services().norm_arxiv(record.get("arxiv_id", ""))
    if doi:
        keys.add("doi:" + doi)
    if arxiv:
        keys.add("arxiv:" + arxiv)
    title = _services().title_key(record.get("title", ""))
    year = record.get("year") or ""
    if title:
        keys.add(f"title:{title}:{year}")
    for alias in (
        record.get("_dedup_aliases", [])
        if isinstance(record.get("_dedup_aliases"), list)
        else []
    ):
        alias = _services().clean_text(alias, 700)
        if alias:
            keys.add(alias)
    return keys


def record_discovery_search_memory(
    db: sqlite3.Connection,
    intent_key: str,
    record: dict,
    run_id: str,
    seen_at: str | None = None,
) -> None:
    """Persist one identity observation for durable, intent-scoped dedupe."""
    if not intent_key or not isinstance(record, dict):
        return
    timestamp = seen_at or _services().now_iso()
    title = _services().clean_text(record.get("title", ""), 600)
    for identity_key in _services().candidate_identity_variants(record):
        db.execute(
            """INSERT INTO discovery_search_memory(intent_key,identity_key,title,first_seen_at,last_seen_at,seen_count,last_run_id)
               VALUES(?,?,?,?,?,1,?)
               ON CONFLICT(intent_key,identity_key) DO UPDATE SET
                 title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE discovery_search_memory.title END,
                 last_seen_at=excluded.last_seen_at,seen_count=discovery_search_memory.seen_count+1,last_run_id=excluded.last_run_id""",
            (intent_key, identity_key, title, timestamp, timestamp, run_id),
        )


def discovery_intent_key(query: str, filters: dict | None = None) -> str:
    """Return a stable scope key for cross-run candidate feedback.

    Presentation controls (sort order, diversity, model screening and source
    parallelism) deliberately do not affect this key.  Only the research
    question and explicit scope boundaries do, so a user can revisit a search
    without relearning that the same paper is irrelevant.
    """
    normalized = _services().normalize_search_options(
        filters if isinstance(filters, dict) else {}
    )
    boundaries = {}
    for key in ("author", "venue", "work_type", "year_from", "year_to"):
        value = normalized.get(key)
        if value not in (None, ""):
            boundaries[key] = value
    excluded = normalized.get("exclude_terms", "")
    if excluded:
        boundaries["exclude_terms"] = sorted(
            {
                _services().clean_text(item, 120).casefold()
                for item in _services().re.split(r"[,;\n]+", excluded)
                if _services().clean_text(item, 120)
            }
        )
    payload = {
        "query": " ".join(_services().clean_text(query, 1000).casefold().split()),
        "boundaries": boundaries,
    }
    digest = (
        _services()
        .hashlib.sha256(
            _services()
            .json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            .encode("utf-8")
        )
        .hexdigest()
    )
    return "intent:" + digest[:32]


def _json_object(value, default: dict | None = None) -> dict:
    """Parse an untrusted JSON object without leaking malformed snapshots."""
    fallback = dict(default or {})
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = _services().json.loads(value or "{}")
    except (TypeError, ValueError, _services().json.JSONDecodeError):
        return fallback
    return parsed if isinstance(parsed, dict) else fallback


def _discovery_feedback_by_key(
    db: sqlite3.Connection, intent_key: str
) -> dict[str, dict]:
    if not intent_key:
        return {}
    rows = db.execute(
        "SELECT candidate_key,decision,note,candidate_title,created_at,updated_at "
        "FROM discovery_candidate_feedback WHERE intent_key=?",
        (intent_key,),
    ).fetchall()
    return {
        row["candidate_key"]: {
            "decision": row["decision"],
            "note": row["note"],
            "candidate_title": row["candidate_title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    }


def _discovery_feedback_examples(
    db: sqlite3.Connection, intent_key: str, limit: int = 10
) -> list[dict]:
    """Return a small, title-only feedback sample for the model prompt."""
    safe_limit = min(max(int(limit), 1), 20)
    rows = db.execute(
        "SELECT candidate_key,decision,candidate_title,updated_at "
        "FROM discovery_candidate_feedback WHERE intent_key=? AND decision<>'new' "
        "ORDER BY updated_at DESC LIMIT ?",
        (intent_key, safe_limit),
    ).fetchall()
    return [
        {
            "decision": row["decision"],
            "title": _services().clean_text(row["candidate_title"], 500),
        }
        for row in rows
    ]


__all__ = ['candidate_identity', 'candidate_identity_variants', 'record_discovery_search_memory', 'discovery_intent_key', '_json_object', '_discovery_feedback_by_key', '_discovery_feedback_examples']
