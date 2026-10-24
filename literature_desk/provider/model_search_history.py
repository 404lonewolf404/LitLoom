"""Reuse previously visible candidates for repeated research intents."""

from __future__ import annotations

from ..services import app_services as _services

def _model_search_history_fill(
    intent_key: str,
    query: str,
    existing: list[dict],
    target_limit: int,
    options: dict | None = None,
) -> list[dict]:
    """Reuse durable candidates when a repeated model search has a short page.

    The model-led path can return a small pool when provider pages overlap or
    a relay ranks only the first few IDs.  The normal connector path already
    has continuation pages, but model search intentionally keeps the tool
    response compact.  Fill the remaining page from *previously visible*
    candidates for the same intent, rather than dropping rows merely because
    they were seen before.  These records remain review candidates and go
    through the same model-web quality gate later in ``run_query``.

    This helper never invents metadata, never imports anything into the
    library, and skips candidates that the user explicitly rejected.  Strict
    ``suppress_seen`` mode remains an explicit fresh-only request.
    """
    options = options or {}
    if (
        options.get("suppress_seen") and not options.get("fill_repeats")
    ) or not intent_key:
        return []
    try:
        target = min(100, max(1, int(target_limit)))
    except (TypeError, ValueError):
        target = 20
    # ``existing`` may contain the same work several times because it was
    # returned by multiple connectors or query lanes.  Comparing its raw
    # length with the requested page size used to make a repeat search look
    # full even when only a few unique papers were available, so the durable
    # history fallback never ran.  Count canonical identities instead; aliases
    # (DOI/arXiv/title) still count as one work.
    existing_unique_keys = {
        _services().candidate_identity(item)
        for item in existing
        if isinstance(item, dict) and _services().candidate_identity(item)
    }
    if len(existing_unique_keys) >= target:
        return []
    existing_keys = set()
    for item in existing:
        if isinstance(item, dict):
            existing_keys.update(_services().candidate_identity_variants(item))
    if not existing_keys:
        existing_keys = {
            _services().candidate_identity(item)
            for item in existing
            if isinstance(item, dict)
        }
    # Candidate rows are deliberately retained as the durable review history.
    # Persisted topic runs are also valid history: long-running saved topics
    # write to ``run_papers`` instead of ``discovery_candidates``.  Include
    # both stores so a repeated AI search cannot become short merely because
    # an earlier run used auto-import mode.  The bounded scan keeps this
    # fallback cheap even after years of searches.
    with _services().DB_LOCK, _services().db_session() as db:
        candidate_rows = db.execute(
            "SELECT id,record_json,created_at FROM discovery_candidates ORDER BY created_at DESC LIMIT 3000"
        ).fetchall()
        # Older candidate snapshots did not persist ``intent_key`` inside
        # record_json.  The search-memory table has always stored the
        # intent-scoped identity, so use it as a durable join key instead of
        # silently dropping those rows from a repeat-search refill.  Without
        # this bridge, a topic with enough historical papers could still
        # render a short page merely because its candidates came from an old
        # schema/version.
        memory_identity_rows = db.execute(
            "SELECT identity_key FROM discovery_search_memory WHERE intent_key=? AND identity_key<>'' LIMIT 5000",
            (intent_key,),
        ).fetchall()
        persisted_rows = db.execute(
            """SELECT p.*,rp.metadata_json AS run_metadata_json,
                      rs.plan_json AS run_plan_json,rs.summary_json AS run_summary_json,
                      rs.created_at AS run_created_at
               FROM run_papers rp
               JOIN papers p ON p.id=rp.paper_id
               JOIN run_snapshots rs ON rs.run_id=rp.run_id
               ORDER BY rs.created_at DESC LIMIT 3000"""
        ).fetchall()
    memory_identity_keys = {
        _services().clean_text(row[0], 700)
        for row in memory_identity_rows
        if _services().clean_text(row[0], 700)
    }
    rows = [
        {"kind": "candidate", "row": row, "created_at": row["created_at"]}
        for row in candidate_rows
    ]
    for row in persisted_rows:
        summary = _services()._json_object(row["run_summary_json"])
        plan = _services()._json_object(row["run_plan_json"])
        row_intent = _services().clean_text(
            summary.get("intent_key") or plan.get("discovery_intent_key"), 100
        )
        if row_intent == intent_key:
            rows.append(
                {"kind": "persisted", "row": row, "created_at": row["run_created_at"]}
            )
    rows.sort(
        key=lambda item: _services().clean_text(item.get("created_at"), 40),
        reverse=True,
    )
    rows = rows[:3000]
    candidates = []
    seen = set(existing_keys)
    facets = _services().extract_search_facets(query)
    for entry in rows:
        row = entry["row"]
        if entry["kind"] == "candidate":
            record = _services()._json_object(row["record_json"])
        else:
            # ``paper_dict`` preserves the normalized library metadata; run
            # metadata adds search-only provenance and prior review labels.
            record = _services().paper_dict(row)
            run_metadata = _services()._json_object(row["run_metadata_json"])
            for key, value in run_metadata.items():
                if key not in record or record.get(key) in (None, "", [], {}):
                    record[key] = value
            record["intent_key"] = intent_key
        # Older rows may contain recursive ``raw`` payloads created before
        # provenance was bounded.  Collapse them while the durable history is
        # being read so one bad row cannot amplify on every repeat search.
        record = _services().compact_candidate_record(record)
        if not isinstance(record, dict):
            continue
        record_intent = _services().clean_text(record.get("intent_key"), 100)
        if record_intent != intent_key:
            # Candidate records from before intent persistence can still be
            # safely reused when at least one canonical identity was observed
            # in the current intent's indexed memory.  Do not broaden this to
            # title-only matching: identity aliases (DOI, arXiv, normalized
            # title/year) are required to avoid cross-topic leakage.
            if record_intent or not (
                _services().candidate_identity_variants(record) & memory_identity_keys
            ):
                continue
            record["intent_key"] = intent_key
        if record.get("review_decision") in {"irrelevant", "ignored"} or record.get(
            "user_decision"
        ) in {"irrelevant", "ignored"}:
            continue
        title = _services().clean_text(record.get("title"), 600)
        url = _services()._model_search_url(
            record.get("url") or record.get("source_url") or record.get("link")
        )
        if not title or not url:
            continue
        if not _services().record_matches_search_options(record, options):
            continue
        identities = _services().candidate_identity_variants(record)
        if not identities or identities & seen:
            continue
        item = dict(record)
        previous_model_label = (
            _services()
            .clean_text(
                item.get("model_relevance_label") or item.get("relevance_label"), 40
            )
            .casefold()
        )
        # Older candidate snapshots predate ``history_quality_accepted``.
        # Their direct/adjacent model verdict is still an auditable acceptance
        # decision for this exact intent, so migrate it in memory on first
        # reuse.  Uncertain legacy rows remain subject to the current lexical
        # evidence guard and are never promoted solely to hit a count.
        legacy_quality_accepted = previous_model_label in {
            "direct",
            "adjacent",
            "relevant",
            "high",
            "直接相关",
            "高相关",
            "邻近方向",
        }
        has_object_task_evidence = (
            _services()._model_web_has_object_task_evidence(item, facets)
            if facets.get("对象") and facets.get("任务")
            else True
        )
        # A history refill is allowed to widen recall only within the same
        # research intent.  For a structured question, reject obvious
        # cross-domain rows before they consume the refill budget; accepted
        # direct/adjacent rows remain eligible even if a newer synonym rule
        # would no longer recognize their wording.
        if (
            not legacy_quality_accepted
            and not item.get("history_quality_accepted")
            and not has_object_task_evidence
        ):
            continue
        triage = _services().score_relevance(item, query, [], [])
        try:
            local_score = int(triage.get("score") or 0)
        except (TypeError, ValueError):
            local_score = 0
        try:
            citation_count = max(0, int(item.get("citation_count") or 0))
        except (TypeError, ValueError):
            citation_count = 0
        item.update(
            {
                "title": title,
                "url": url,
                "source": "model_web",
                "_previously_seen": True,
                "_history_fill": True,
                # This row was already accepted into the durable review queue for
                # the same intent.  Carry the decision forward so the later
                # model-web quality pass does not mistake a repeat for a new,
                # lower-confidence hit and remove it again.
                "history_quality_accepted": bool(
                    record.get("history_quality_accepted") or legacy_quality_accepted
                ),
                "external_id": _services().clean_text(
                    item.get("external_id")
                    or item.get("doi")
                    or item.get("arxiv_id")
                    or url,
                    500,
                ),
            }
        )
        # Keep a stable source citation even for legacy candidate rows that
        # predate provider_sources/search_paths.
        item.setdefault("source_citations", [url])
        item.setdefault(
            "provider_sources",
            [
                {
                    "source": "history",
                    "external_id": item.get("external_id", ""),
                    "url": url,
                }
            ],
        )
        item.setdefault("search_paths", [])
        candidates.append(
            {
                "_history_sort": (
                    (
                        0
                        if (
                            item.get("history_quality_accepted")
                            or legacy_quality_accepted
                        )
                        else 1
                    ),
                    0 if has_object_task_evidence else 1,
                    -local_score,
                    -citation_count,
                    _services().clean_text(entry.get("created_at"), 40),
                ),
                "_history_item": item,
            }
        )
        seen.update(identities)
    candidates.sort(key=lambda value: value["_history_sort"])
    return [
        value["_history_item"]
        for value in candidates[: max(0, target - len(existing_unique_keys))]
    ]


__all__ = ['_model_search_history_fill']
