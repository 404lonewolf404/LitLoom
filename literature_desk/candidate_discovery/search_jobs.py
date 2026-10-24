"""Background search jobs and cadence helpers."""

from __future__ import annotations

from ..services import app_services as _services

def launch_query_job(
    query: str,
    sources: list[str],
    limit: int = 20,
    query_id: str = "",
    scheduled: bool = False,
    name: str = "",
    options: dict | None = None,
    request_id: str = "",
    plan_override: dict | None = None,
) -> dict:
    """Run a user initiated search in the background and return its durable ID."""
    q = _services().clean_text(query, 1000)
    valid_sources = [
        source
        for source in sources
        if source in _services().SOURCE_NAMES and source in _services().SOURCE_SEARCH
    ]
    # Normalize browser and legacy saved-run options before persisting the
    # initial snapshot.  This prevents an old client that omitted
    # ``fill_repeats`` from turning the normal review queue into strict
    # fresh-only mode.
    options = _services().prepare_user_search_options(options)
    normalized_options = _services().normalize_search_options(options)
    if not q:
        raise ValueError("请输入研究问题或关键词")
    if not valid_sources and normalized_options.get("search_backend") != "model_web":
        raise ValueError(
            "请至少选择一个学术来源；如果希望由模型自主选择来源，请切换到 AI 工具检索"
        )
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
        db.execute(
            "INSERT OR IGNORE INTO runs(id,query_id,query_name,status,started_at,sources_json,is_scheduled) VALUES(?,?,?,?,?,?,?)",
            (
                run_id,
                query_id,
                name or q,
                "queued",
                started,
                _services().json.dumps(valid_sources),
                int(scheduled),
            ),
        )
        db.execute(
            "INSERT OR IGNORE INTO sync_jobs(id,query_id,run_id,status,stage,attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, query_id, run_id, "queued", "queued", 0, started, started),
        )
        initial_plan = dict(plan_override) if isinstance(plan_override, dict) else {}
        initial_plan["search_options"] = normalized_options
        initial_plan["result_limit"] = limit
        candidate_mode = not _services().search_persists_results(options, scheduled)
        initial_summary = {
            "candidate_mode": candidate_mode,
            "intent_key": _services().discovery_intent_key(
                q, initial_plan["search_options"]
            ),
        }
        db.execute(
            "INSERT OR IGNORE INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)",
            (
                run_id,
                q,
                _services().json.dumps(initial_plan, ensure_ascii=False),
                _services().json.dumps(initial_summary),
                started,
            ),
        )

    def worker():
        try:
            execution_plan = plan_override
            # Ordinary searches should understand a natural-language research
            # description too; users should not have to click a separate
            # extension button just to translate “food volume estimation”.
            # Keep this bounded and background-only.  The local semantic
            # planner remains the fallback and is always used when no model is
            # configured or the provider returns malformed output.
            natural_language = (
                bool(
                    _services().re.search(r"[\u4e00-\u9fff]", q)
                    and len(_services().re.findall(r"[\u4e00-\u9fff]", q)) >= 8
                )
                or len(_services().re.findall(r"[A-Za-z]{3,}", q)) >= 6
            )
            if (
                execution_plan is None
                and normalized_options.get("search_backend") != "model_web"
                and normalized_options.get("ai_expand", True)
                and natural_language
                and _services().model_settings().get("enabled")
                and _services().model_api_key()
            ):
                with _services().DB_LOCK, _services().db_session() as db:
                    db.execute(
                        "UPDATE sync_jobs SET stage=?,updated_at=? WHERE id=?",
                        ("semantic_planning", _services().now_iso(), run_id),
                    )
                try:
                    execution_plan = _services().agent_discovery_plan(
                        q, valid_sources, normalized_options
                    )
                except Exception as planning_error:
                    # Preserve the diagnostic in the run audit without making
                    # a provider hiccup turn a usable search into a failure.
                    execution_plan = None
                    with _services().DB_LOCK, _services().db_session() as db:
                        _services().record_audit(
                            db,
                            "sync.semantic_planning_fallback",
                            "run",
                            run_id,
                            {"error": _services().clean_text(str(planning_error), 300)},
                        )
            _services().run_query(
                q,
                valid_sources,
                limit,
                query_id,
                scheduled,
                name,
                options,
                request_id=run_id,
                plan_override=execution_plan,
            )
        except Exception as exc:
            message = str(exc)[:1000]
            completed = _services().now_iso()
            with _services().DB_LOCK, _services().db_session() as db:
                run = db.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
                if run:
                    db.execute(
                        "UPDATE runs SET status='failed',completed_at=?,error=? WHERE id=?",
                        (completed, message, run_id),
                    )
                    db.execute(
                        "UPDATE sync_jobs SET status='failed',stage='failed',last_error=?,updated_at=? WHERE id=?",
                        (message, completed, run_id),
                    )
                else:
                    db.execute(
                        "INSERT INTO runs(id,query_id,query_name,status,started_at,completed_at,error,is_scheduled) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            query_id,
                            name or q,
                            "failed",
                            completed,
                            completed,
                            message,
                            int(scheduled),
                        ),
                    )
                    db.execute(
                        "INSERT INTO sync_jobs(id,query_id,run_id,status,stage,last_error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            query_id,
                            run_id,
                            "failed",
                            "failed",
                            message,
                            completed,
                            completed,
                        ),
                    )
                    db.execute(
                        "INSERT INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)",
                        (run_id, q, "{}", "{}", completed),
                    )
        finally:
            _services()._unregister_background_thread()

    search_thread = _services().threading.Thread(
        target=worker, name=f"literature-search-{run_id[:8]}", daemon=True
    )
    _services()._register_background_thread(search_thread)
    search_thread.start()
    # Keep the immediate response backward compatible with callers that pass
    # an AI plan, while the durable snapshot stores execution options too.
    return {
        "id": run_id,
        "status": "queued",
        "query": q,
        "query_name": name or q,
        "source_names": valid_sources,
        "papers": [],
        "accepted_count": 0,
        "plan": plan_override if isinstance(plan_override, dict) else initial_plan,
        "candidate_mode": candidate_mode,
    }


def get_query_cadence(query_id: str) -> int:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT cadence_hours FROM saved_queries WHERE id=?", (query_id,)
        ).fetchone()
        return int(row[0]) if row else 168


__all__ = [
    "library_relation_variants",
    "library_term_variants",
    "search_query_plan",
    "normalize_search_options",
    "prepare_user_search_options",
    "search_persists_results",
    "record_matches_search_options",
    "rank_fusion_score",
    "diversify_results",
    "_prioritize_ai_search_variants",
    "_prefetch_source_variants",
    "run_query",
    "launch_query_job",
    "get_query_cadence",
]
