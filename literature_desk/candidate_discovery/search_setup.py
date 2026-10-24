"""Initialize a durable discovery run and its execution plan."""

from __future__ import annotations

from types import SimpleNamespace

from ..services import app_services as _services

def _prepare_search_run(
    query: str, sources: list[str], limit: int, query_id: str, scheduled: bool,
    name: str, options: dict | None, request_id: str, plan_override: dict | None,
) -> SimpleNamespace:
    q = _services().clean_text(query, 1000)
    sources = [
        s
        for s in sources
        if s in _services().SOURCE_NAMES and s in _services().SOURCE_SEARCH
    ]
    search_options = _services().normalize_search_options(options)
    if not q:
        raise ValueError("请输入研究问题或关键词")
    if not sources and search_options.get("search_backend") != "model_web":
        raise ValueError(
            "请至少选择一个学术来源；如果希望由模型自主选择来源，请切换到 AI 工具检索"
        )
    # Scheduled discovery is a review queue too; auto_add must be explicit.
    persist_results = _services().search_persists_results(options, scheduled)
    # Agent runs may provide a reviewed PlannerAgent plan.  Use it as the
    # execution contract instead of silently regenerating a different set of
    # query variants.  Invalid/partial plans still fall back safely to the
    # deterministic local planner used by ordinary searches.
    plan = (
        plan_override
        if isinstance(plan_override, dict)
        and isinstance(plan_override.get("query_variants"), list)
        and plan_override.get("query_variants")
        else _services().search_query_plan(q, sources, search_options)
    )
    if isinstance(plan_override, dict):
        q = _services().clean_text(
            plan_override.get("original_query") or plan_override.get("query") or q, 1000
        )
        planned_sources = [
            item.get("id")
            for item in (plan_override.get("sources") or [])
            if isinstance(item, dict) and item.get("id") in _services().SOURCE_SEARCH
        ]
        if planned_sources:
            sources = list(dict.fromkeys(planned_sources))
        plan.setdefault("original_query", q)
        plan.setdefault("query", q)
    plan.setdefault("search_options", search_options)
    plan["result_limit"] = limit
    intent_key = _services().discovery_intent_key(
        q, plan.get("filters") if isinstance(plan, dict) else search_options
    )
    plan["discovery_intent_key"] = intent_key
    # Read the durable topic memory before fetching.  When a topic has
    # already produced candidates, the first page is often dominated by
    # repeats; enable one bounded refill page for this run only.  New topics
    # retain the fast one-page path.
    with _services().DB_LOCK, _services().db_session() as db:
        prior_memory_count = int(
            db.execute(
                "SELECT COUNT(*) FROM discovery_search_memory WHERE intent_key=?",
                (intent_key,),
            ).fetchone()[0]
            or 0
        )
        prior_intent_identities = {
            _services().clean_text(row[0], 700)
            for row in db.execute(
                "SELECT identity_key FROM discovery_search_memory WHERE intent_key=? AND identity_key<>''",
                (intent_key,),
            )
            if _services().clean_text(row[0], 700)
        }
    refill_repeats = bool(
        search_options.get("avoid_repeats", True) and prior_memory_count > 0
    )
    plan["repeat_refill"] = {
        "enabled": refill_repeats,
        "prior_memory_count": prior_memory_count,
        # Repeated topics may have several provider pages occupied by the
        # same historical works. Seven bounded continuation pages keep the
        # requested result size from collapsing while the no-progress guard
        # still stops connectors that ignore offsets.
        "max_extra_primary_pages": 7 if refill_repeats else 0,
    }
    try:
        run_id = (
            str(_services().uuid.UUID(request_id))
            if request_id
            else str(_services().uuid.uuid4())
        )
    except (ValueError, AttributeError):
        raise ValueError("检索请求 ID 无效，请重新发起检索")
    started = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        existing = db.execute(
            "SELECT started_at FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if existing:
            started = existing["started_at"] or started
            db.execute(
                "UPDATE runs SET query_id=?,query_name=?,status='running',sources_json=?,is_scheduled=? WHERE id=?",
                (
                    query_id,
                    name or q,
                    _services().json.dumps(sources),
                    int(scheduled),
                    run_id,
                ),
            )
            db.execute(
                "UPDATE sync_jobs SET query_id=?,status='running',stage='planning',attempts=attempts+1,last_error='',updated_at=? WHERE id=?",
                (query_id, _services().now_iso(), run_id),
            )
            db.execute(
                "UPDATE run_snapshots SET query=?,plan_json=?,summary_json=? WHERE run_id=?",
                (
                    q,
                    _services().json.dumps(plan, ensure_ascii=False),
                    _services().json.dumps(
                        {
                            "candidate_mode": not persist_results,
                            "intent_key": intent_key,
                        }
                    ),
                    run_id,
                ),
            )
        else:
            db.execute(
                "INSERT INTO runs(id,query_id,query_name,status,started_at,sources_json,is_scheduled) VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    query_id,
                    name or q,
                    "running",
                    started,
                    _services().json.dumps(sources),
                    int(scheduled),
                ),
            )
            db.execute(
                "INSERT INTO sync_jobs(id,query_id,run_id,status,stage,attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, query_id, run_id, "running", "planning", 1, started, started),
            )
            db.execute(
                "INSERT INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)",
                (
                    run_id,
                    q,
                    _services().json.dumps(plan, ensure_ascii=False),
                    _services().json.dumps(
                        {
                            "candidate_mode": not persist_results,
                            "intent_key": intent_key,
                        }
                    ),
                    started,
                ),
            )
        _services().record_audit(
            db,
            "sync.started",
            "run",
            run_id,
            {"query_id": query_id, "sources": sources, "scheduled": bool(scheduled)},
        )
    return SimpleNamespace(**locals())
