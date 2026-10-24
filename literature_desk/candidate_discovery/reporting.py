"""Discovery run snapshots, analytics, diagnostics, and evaluation reports."""

from __future__ import annotations

from ..services import app_services as _services


def evaluation_summary() -> dict:
    """Compute ranking quality from explicit user judgments.

    Only ``relevant`` and ``irrelevant`` judgments are evaluable labels;
    ``ignored`` and ``later`` remain workflow states and are excluded from
    precision calculations.  The metric is intentionally transparent and
    local so a future embedding ranker can be compared against this baseline.
    """
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            """SELECT qm.query_id,qm.paper_id,qm.score,qm.user_decision,qm.last_seen_at,
            COALESCE(NULLIF(sq.name,''),sq.query,qm.query_id) AS query_name
            FROM query_matches qm LEFT JOIN saved_queries sq ON sq.id=qm.query_id
            WHERE user_decision IN ('relevant','irrelevant')
            ORDER BY query_id,score DESC,last_seen_at DESC"""
        ).fetchall()
    groups = {}
    for row in rows:
        groups.setdefault(row["query_id"], []).append(dict(row))
    per_query = []
    for query_id, items in groups.items():
        ranked = sorted(
            items,
            key=lambda item: (item.get("score", 0), item.get("last_seen_at", "")),
            reverse=True,
        )
        metrics = {
            "query_id": query_id,
            "query_name": ranked[0].get("query_name") or query_id,
            "judged": len(ranked),
            "relevant": sum(item["user_decision"] == "relevant" for item in ranked),
        }
        for k in (5, 10):
            top = ranked[:k]
            metrics[f"precision_at_{k}"] = (
                round(
                    sum(item["user_decision"] == "relevant" for item in top) / len(top),
                    4,
                )
                if top
                else None
            )
        per_query.append(metrics)
    judged = len(rows)
    relevant = sum(row["user_decision"] == "relevant" for row in rows)
    result = {
        "status": "ready" if judged >= 5 else "insufficient_samples",
        "judged_matches": judged,
        "relevant_matches": relevant,
        "negative_matches": judged - relevant,
        "query_count": len(per_query),
        "precision_at_5": (
            round(
                sum(
                    item["precision_at_5"]
                    for item in per_query
                    if item["precision_at_5"] is not None
                )
                / max(1, sum(item["precision_at_5"] is not None for item in per_query)),
                4,
            )
            if per_query
            else None
        ),
        "precision_at_10": (
            round(
                sum(
                    item["precision_at_10"]
                    for item in per_query
                    if item["precision_at_10"] is not None
                )
                / max(
                    1, sum(item["precision_at_10"] is not None for item in per_query)
                ),
                4,
            )
            if per_query
            else None
        ),
        "judged_precision": round(relevant / judged, 4) if judged else None,
        "per_query": per_query[:50],
    }
    return result


def analytics_summary() -> dict:
    """Build a small, reproducible product/learning dashboard from local facts."""
    now = _services().datetime.now(_services().timezone.utc).replace(microsecond=0)
    horizon = (now - _services().timedelta(days=90)).isoformat()
    month_horizon = (now - _services().timedelta(days=30)).isoformat()
    with _services().DB_LOCK, _services().db_session() as db:
        total = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        status_rows = db.execute(
            "SELECT status,COUNT(*) AS n FROM papers GROUP BY status"
        ).fetchall()
        added_rows = db.execute(
            "SELECT created_at FROM papers WHERE created_at>=?", (horizon,)
        ).fetchall()
        read_rows = db.execute(
            "SELECT updated_at FROM papers WHERE status='read' AND updated_at>=?",
            (horizon,),
        ).fetchall()
        run_rows = db.execute(
            "SELECT status,new_count,duplicate_count,sources_json,source_details_json,started_at,completed_at FROM runs WHERE started_at>=?",
            (month_horizon,),
        ).fetchall()
        topic_rows = db.execute(
            "SELECT COALESCE(NULLIF(topic,''),'未分类') AS topic,COUNT(*) AS n FROM papers GROUP BY topic ORDER BY n DESC LIMIT 10"
        ).fetchall()
        feedback_rows = db.execute(
            "SELECT user_decision,COUNT(*) AS n FROM query_matches GROUP BY user_decision"
        ).fetchall()
        match_total = db.execute("SELECT COUNT(*) FROM query_matches").fetchone()[0]
        audit_events_30d = db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE created_at>=?", (month_horizon,)
        ).fetchone()[0]
        schema_row = db.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()
        attachments = db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        active_queries = db.execute(
            "SELECT COUNT(*) FROM saved_queries WHERE active=1"
        ).fetchone()[0]
        structured_reading_count = db.execute(
            "SELECT COUNT(*) FROM reading_cards WHERE updated_at>=?", (horizon,)
        ).fetchone()[0]
        reading_card_total = db.execute(
            "SELECT COUNT(*) FROM reading_cards"
        ).fetchone()[0]
        reading_card_fields = db.execute(
            "SELECT research_question,contribution,method,datasets,baselines,results,limitations,reproducibility,evidence_notes,relevance_to_me,next_steps FROM reading_cards"
        ).fetchall()

    def week_key(value: str) -> str:
        try:
            return (
                _services()
                .datetime.fromisoformat(value.replace("Z", "+00:00"))
                .date()
                .isoformat()
            )
        except (TypeError, ValueError):
            return ""

    weeks = []
    monday = now.date() - _services().timedelta(days=now.date().weekday())
    for index in range(11, -1, -1):
        start = monday - _services().timedelta(days=index * 7)
        weeks.append({"week": start.isoformat(), "added": 0, "read": 0})
    week_map = {item["week"]: item for item in weeks}
    for row in added_rows:
        day = week_key(row["created_at"])
        if day:
            start = _services().datetime.fromisoformat(
                day
            ).date() - _services().timedelta(
                days=_services().datetime.fromisoformat(day).date().weekday()
            )
            if start.isoformat() in week_map:
                week_map[start.isoformat()]["added"] += 1
    for row in read_rows:
        day = week_key(row["updated_at"])
        if day:
            start = _services().datetime.fromisoformat(
                day
            ).date() - _services().timedelta(
                days=_services().datetime.fromisoformat(day).date().weekday()
            )
            if start.isoformat() in week_map:
                week_map[start.isoformat()]["read"] += 1
    source_health = {}
    detail_runs = 0
    detail_missing_runs = 0
    durations = []
    runs_success = runs_partial = runs_failed = new_count = duplicate_count = 0
    raw_fetched_total = accepted_total = filtered_total = 0
    for row in run_rows:
        status = row["status"]
        runs_success += int(status == "success")
        runs_partial += int(status == "partial")
        runs_failed += int(status == "failed")
        new_count += row["new_count"] or 0
        duplicate_count += row["duplicate_count"] or 0
        try:
            if row["started_at"] and row["completed_at"]:
                duration = (
                    _services().datetime.fromisoformat(row["completed_at"])
                    - _services().datetime.fromisoformat(row["started_at"])
                ).total_seconds()
                if duration >= 0:
                    durations.append(duration)
        except (TypeError, ValueError):
            pass
        try:
            details = _services().json.loads(row["source_details_json"] or "[]")
        except (TypeError, _services().json.JSONDecodeError):
            details = []
        # Schema 6 and earlier only stored source names. Keep those runs in
        # the window, but do not invent fetched/accepted counts for them.
        if isinstance(details, list) and any(
            isinstance(detail, dict) for detail in details
        ):
            detail_runs += 1
        else:
            detail_missing_runs += 1
            details = []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            name = detail.get("source", "unknown")
            item = source_health.setdefault(
                name,
                {
                    "source": name,
                    "runs": 0,
                    "success": 0,
                    "failed": 0,
                    "fetched": 0,
                    "accepted": 0,
                    "filtered": 0,
                },
            )
            item["runs"] += 1
            item["success"] += int(detail.get("status") == "ok")
            item["failed"] += int(detail.get("status") != "ok")
            item["fetched"] += int(detail.get("fetched") or 0)
            item["accepted"] += int(
                detail.get("accepted", detail.get("fetched", 0)) or 0
            )
            item["filtered"] += int(detail.get("filtered", 0) or 0)
            raw_fetched_total += int(detail.get("fetched") or 0)
            accepted_total += int(detail.get("accepted", detail.get("fetched", 0)) or 0)
            filtered_total += int(detail.get("filtered", 0) or 0)
    run_total = len(run_rows)
    evaluation = _services().evaluation_summary()
    feedback_coverage = (
        round(
            (
                sum(
                    int(row["n"])
                    for row in feedback_rows
                    if row["user_decision"] in {"relevant", "irrelevant", "ignored"}
                )
                / match_total
            )
            * 100,
            1,
        )
        if match_total
        else 0
    )
    dedup_rate = (
        round((duplicate_count / (new_count + duplicate_count)) * 100, 1)
        if (new_count + duplicate_count)
        else 0
    )
    acceptance_rate = (
        round((accepted_total / raw_fetched_total) * 100, 1) if raw_fetched_total else 0
    )
    reading_field_total = len(reading_card_fields) * len(
        _services().READING_CARD_FIELDS
    )
    reading_field_completed = sum(
        bool(row[field].strip())
        for row in reading_card_fields
        for field in _services().READING_CARD_FIELDS
    )
    reading_card_coverage = round((reading_card_total / total) * 100, 1) if total else 0
    reading_field_completion = (
        round((reading_field_completed / reading_field_total) * 100, 1)
        if reading_field_total
        else 0
    )
    p95 = _services().runtime_metrics_summary().get("p95_ms", 0)
    optimization_actions = []
    if not run_total:
        optimization_actions.append(
            {
                "priority": "P1",
                "area": "检索基线",
                "signal": "尚无同步运行样本",
                "action": "先保存一个窄主题并运行 3 次，再比较来源成功率、去重率和 Precision@5。",
                "evidence": "runs_30d=0",
            }
        )
    if detail_missing_runs:
        optimization_actions.append(
            {
                "priority": "P1",
                "area": "数据完整性",
                "signal": f"{detail_missing_runs} 次历史运行缺少来源详情",
                "action": "使用新版本重新运行主题以建立可比较的抓取、过滤和接受基线；历史数量保持未知，不回填估算值。",
                "evidence": "source_details_json=missing",
            }
        )
    if evaluation.get("status") != "ready":
        optimization_actions.append(
            {
                "priority": "P1",
                "area": "质量评测",
                "signal": f"明确判断样本 {evaluation.get('judged_matches', 0)} 个",
                "action": "优先标记相关/忽略，样本达到 5 个后再调整排序权重或接入 embedding。",
                "evidence": "evaluation.status=insufficient_samples",
            }
        )
    if feedback_coverage < 30 and match_total:
        optimization_actions.append(
            {
                "priority": "P1",
                "area": "反馈闭环",
                "signal": f"反馈覆盖率 {feedback_coverage}%",
                "action": "在发现页完成首轮人工分诊，避免用未标注数据判断检索质量。",
                "evidence": f"judged_matches/matches={feedback_coverage}%",
            }
        )
    if total and reading_card_coverage < 30:
        optimization_actions.append(
            {
                "priority": "P2",
                "area": "阅读证据",
                "signal": f"结构化阅读卡覆盖率 {reading_card_coverage}%",
                "action": "优先为正在读和已读论文补齐问题、贡献、方法、结果与局限，后续推荐会更容易解释。",
                "evidence": f"reading_cards/papers={reading_card_coverage}%",
            }
        )
    if dedup_rate >= 45:
        optimization_actions.append(
            {
                "priority": "P2",
                "area": "召回成本",
                "signal": f"去重率 {dedup_rate}%",
                "action": "检查查询变体和来源重叠；可降低深度检索频率或保留更强的来源优先级。",
                "evidence": f"duplicates/(new+duplicates)={dedup_rate}%",
            }
        )
    if raw_fetched_total and acceptance_rate < 20:
        optimization_actions.append(
            {
                "priority": "P2",
                "area": "过滤策略",
                "signal": f"候选接受率 {acceptance_rate}%",
                "action": "检查年份、作者、venue、排除词是否过严，并对低摘要覆盖来源单独评估。",
                "evidence": f"accepted/fetched={acceptance_rate}%",
            }
        )
    failed_sources = [
        item["source"]
        for item in source_health.values()
        if item.get("runs")
        and item.get("failed", 0) / max(1, item.get("runs", 0)) >= 0.5
    ]
    if failed_sources:
        optimization_actions.append(
            {
                "priority": "P1",
                "area": "连接器可靠性",
                "signal": "、".join(failed_sources) + " 失败率较高",
                "action": "暂停或降低该来源频率，确认其余来源成功后再判断是否真的没有新论文。",
                "evidence": "source_health.failed/runs>=50%",
            }
        )
    if p95 > 1500:
        optimization_actions.append(
            {
                "priority": "P1",
                "area": "性能 SLO",
                "signal": f"当前 p95 {p95} ms",
                "action": "优先检查外部连接器超时、检索变体数量和 PDF 派生任务，保持模型调用与核心事务解耦。",
                "evidence": "runtime.p95_ms>1500",
            }
        )
    if not optimization_actions:
        optimization_actions.append(
            {
                "priority": "P3",
                "area": "持续改进",
                "signal": "当前指标没有触发异常门槛",
                "action": "保持每周复盘，使用同一批人工判断比较检索策略版本。",
                "evidence": "all quality gates within observed thresholds",
            }
        )
    retrieval_funnel = {
        "fetched": raw_fetched_total,
        "filtered": filtered_total,
        "accepted": accepted_total,
        "new": new_count,
        "duplicates": duplicate_count,
        "filter_rate_pct": (
            round((filtered_total / raw_fetched_total) * 100, 1)
            if raw_fetched_total
            else 0
        ),
        "acceptance_rate_pct": acceptance_rate,
        "dedup_rate_pct": dedup_rate,
        "data_quality": (
            "complete" if detail_missing_runs == 0 else "historical_detail_missing"
        ),
    }
    data_quality = {
        "status": (
            "complete"
            if detail_missing_runs == 0
            else ("partial" if detail_runs else "historical_detail_missing")
        ),
        "runs_with_details": detail_runs,
        "runs_missing_details": detail_missing_runs,
        "message": (
            "所有观测窗口内的运行都包含来源详情。"
            if detail_missing_runs == 0
            else "部分历史运行只有来源名称，无法恢复抓取/过滤数量；新运行会自动补齐详情。"
        ),
    }
    development_cycle = {
        "baseline": "rankfusion-v2 + explainable lexical triage",
        "observation_window_days": 30,
        "feedback_samples": evaluation.get("judged_matches", 0),
        "next_gate": (
            "样本不足"
            if evaluation.get("status") != "ready"
            else "比较 Precision@5/10 后再调整模型或权重"
        ),
        "gates": {"p95_ms": 1500, "server_error_rate_pct": 1.0, "minimum_judgments": 5},
    }
    return {
        "generated_at": now.isoformat(),
        "metrics": {
            "total_papers": total,
            "backlog": sum(
                int(row["n"])
                for row in status_rows
                if row["status"] in {"inbox", "to_read", "later"}
            ),
            "reading": next(
                (row["n"] for row in status_rows if row["status"] == "reading"), 0
            ),
            "read": next(
                (row["n"] for row in status_rows if row["status"] == "read"), 0
            ),
            "new_30d": sum(
                1 for row in added_rows if row["created_at"] >= month_horizon
            ),
            "read_30d": sum(
                1 for row in read_rows if row["updated_at"] >= month_horizon
            ),
            "runs_30d": run_total,
            "success_rate": (
                round((runs_success / run_total) * 100, 1) if run_total else 0
            ),
            "dedup_rate": dedup_rate,
            "attachments": attachments,
            "active_queries": active_queries,
            "feedback_coverage": feedback_coverage,
            "audit_events_30d": audit_events_30d,
            "avg_run_seconds": (
                round(sum(durations) / len(durations), 2) if durations else 0
            ),
            "raw_fetched_30d": raw_fetched_total,
            "accepted_candidates_30d": accepted_total,
            "filtered_candidates_30d": filtered_total,
            "candidate_acceptance_rate": acceptance_rate,
            "schema_version": schema_row[0] if schema_row else "unknown",
            "reading_card_coverage": reading_card_coverage,
            "structured_reading_count": structured_reading_count,
            "reading_card_total": reading_card_total,
            "reading_field_completion": reading_field_completion,
        },
        "status": {row["status"]: row["n"] for row in status_rows},
        "topics": [dict(row) for row in topic_rows],
        "feedback": {row["user_decision"]: row["n"] for row in feedback_rows},
        "runs": {
            "total": run_total,
            "success": runs_success,
            "partial": runs_partial,
            "failed": runs_failed,
        },
        "source_health": list(source_health.values()),
        "evaluation": evaluation,
        "retrieval_funnel": retrieval_funnel,
        "data_quality": data_quality,
        "optimization_actions": optimization_actions,
        "development_cycle": development_cycle,
        "weekly": weeks,
        "runtime": _services().runtime_metrics_summary(),
        "operational": {
            "audit_events_30d": audit_events_30d,
            "avg_run_seconds": (
                round(sum(durations) / len(durations), 2) if durations else 0
            ),
            "schema_version": schema_row[0] if schema_row else "unknown",
        },
        "reading_evidence": {
            "card_count": reading_card_total,
            "coverage_pct": reading_card_coverage,
            "field_completion_pct": reading_field_completion,
            "recent_cards_90d": structured_reading_count,
            "fields": list(_services().READING_CARD_FIELDS),
        },
    }


def health_summary() -> dict:
    """Expose an operator-friendly readiness snapshot without leaking secrets."""
    database = {
        "status": "ok",
        "path": (
            _services().postgres_database_label(_services().postgres_database_url())
            if _services().using_postgres()
            else str(_services().DB_PATH)
        ),
        "integrity": "ok",
        "schema_version": "",
    }
    try:
        with _services().DB_LOCK, _services().db_session() as db:
            integrity = _services().database_integrity_check(db)
            row = db.execute(
                "SELECT value FROM app_meta WHERE key='schema_version'"
            ).fetchone()
            database["integrity"] = integrity
            database["schema_version"] = row[0] if row else "unknown"
            database["papers"] = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            database["audit_events"] = db.execute(
                "SELECT COUNT(*) FROM audit_events"
            ).fetchone()[0]
            if integrity != "ok":
                database["status"] = "error"
    except Exception as exc:
        database.update({"status": "error", "integrity": str(exc)[:300]})
    scheduler = {
        "status": "not_started",
        "started_at": _services().SCHEDULER_STARTED_AT,
        "last_tick_at": _services().SCHEDULER_LAST_TICK,
    }
    if _services().SCHEDULER_STARTED_AT:
        scheduler["status"] = (
            "starting" if not _services().SCHEDULER_LAST_TICK else "ok"
        )
        if _services().SCHEDULER_LAST_TICK:
            try:
                age = (
                    _services().datetime.now(_services().timezone.utc)
                    - _services().datetime.fromisoformat(
                        _services().SCHEDULER_LAST_TICK
                    )
                ).total_seconds()
                scheduler["age_seconds"] = max(0, round(age))
                if age > 150:
                    scheduler["status"] = "stale"
            except ValueError:
                scheduler["status"] = "unknown"
    latest_backup = ""
    backup_dir = _services().active_backup_dir()
    if backup_dir.exists():
        backups = sorted(
            [
                *backup_dir.glob("researchdesk-backup-*.zip"),
                *backup_dir.glob("researchdesk-auto-*.zip"),
            ],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if backups:
            latest_backup = (
                _services()
                .datetime.fromtimestamp(
                    backups[0].stat().st_mtime, _services().timezone.utc
                )
                .replace(microsecond=0)
                .isoformat()
            )
    return {
        "status": "ok" if database["status"] == "ok" else "degraded",
        "ready": database["status"] == "ok",
        "app_version": _services().APP_VERSION,
        "process_started_at": _services().PROCESS_STARTED_AT,
        "generated_at": _services().now_iso(),
        "database": database,
        "scheduler": scheduler,
        "latest_backup_at": latest_backup,
    }


def diagnostics_summary() -> dict:
    """Return a safe release/readiness snapshot for operators and demos.

    This endpoint intentionally combines facts from the health probe, database
    quality checks, connector catalog and process SLOs. It never includes API
    keys, request payloads, notes or PDF contents.
    """
    health = _services().health_summary()
    with _services().DB_LOCK, _services().db_session() as db:
        total_papers = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        missing_abstract = db.execute(
            "SELECT COUNT(*) FROM papers WHERE trim(abstract)='' "
        ).fetchone()[0]
        missing_identity = db.execute(
            "SELECT COUNT(*) FROM papers WHERE trim(doi)='' AND trim(arxiv_id)='' "
        ).fetchone()[0]
        source_observations = db.execute(
            "SELECT COUNT(*) FROM source_records"
        ).fetchone()[0]
        duplicate_title_keys = db.execute(
            # PostgreSQL does not allow SELECT aliases in HAVING. Keep this
            # query portable because diagnostics must work against both the
            # SQLite migration backend and the production PostgreSQL backend.
            "SELECT COUNT(*) FROM (SELECT title_key,year FROM papers GROUP BY title_key,year HAVING COUNT(*)>1)"
        ).fetchone()[0]
        matches = db.execute("SELECT COUNT(*) FROM query_matches").fetchone()[0]
        judged = db.execute(
            "SELECT COUNT(*) FROM query_matches WHERE user_decision IN ('relevant','irrelevant','ignored')"
        ).fetchone()[0]
        schema_row = db.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()
    source_catalog = {
        "arxiv": {"role": "最新预印本", "access": "public", "authentication": "none"},
        "openalex": {
            "role": "跨学科元数据与引用",
            "access": "public",
            "authentication": "none",
        },
        "crossref": {
            "role": "正式出版 DOI 元数据",
            "access": "public",
            "authentication": "none",
        },
        "europepmc": {
            "role": "医学与生命科学",
            "access": "public",
            "authentication": "none",
        },
        "semanticscholar": {
            "role": "语义检索与相似工作",
            "access": "optional",
            "authentication": "public quota or key",
        },
        "dblp": {
            "role": "计算机科学会议与作者索引",
            "access": "optional",
            "authentication": "public; network challenge possible",
        },
        "openreview": {
            "role": "开放评审会议与 workshop 最新投稿",
            "access": "optional",
            "authentication": "public; endpoint availability varies",
        },
    }
    actor = _services().AUTH_ACTOR.get() or {}
    rollout_subject = actor.get("workspace_id") or "local"
    return {
        "generated_at": _services().now_iso(),
        "configuration": _services().configuration_snapshot(
            root=_services().ROOT,
            postgres_backend=_services().using_postgres(),
        ),
        "release": {
            "app_version": _services().APP_VERSION,
            "schema_version": schema_row[0] if schema_row else "unknown",
            "python_version": _services().sys.version.split()[0],
            "api_contract": "openapi.yaml",
            "quality_gate": "scripts/verify.ps1",
            "rollout": _services().release_snapshot(rollout_subject),
        },
        "health": health,
        "connectors": source_catalog,
        "data_quality": {
            "papers": total_papers,
            "source_observations": source_observations,
            "missing_abstract": missing_abstract,
            "missing_abstract_pct": (
                round((missing_abstract / total_papers) * 100, 2) if total_papers else 0
            ),
            "missing_strong_identifier": missing_identity,
            "missing_strong_identifier_pct": (
                round((missing_identity / total_papers) * 100, 2) if total_papers else 0
            ),
            "duplicate_title_year_groups": duplicate_title_keys,
            "query_matches": matches,
            "judged_matches": judged,
            "feedback_coverage_pct": (
                round((judged / matches) * 100, 2) if matches else 0
            ),
        },
        "slo": _services().runtime_metrics_summary(),
        "slo_targets": {"p95_ms": 1500, "server_error_rate_pct": 1.0},
    }


def run_list_snapshots(db, limit: int = 100) -> list[dict]:
    rows = db.execute(
        """SELECT r.*,rs.query AS snapshot_query,rs.summary_json AS candidate_summary_json,
                  (SELECT COUNT(*) FROM discovery_candidates dc WHERE dc.run_id=r.id) AS candidate_row_count,
                  (SELECT COUNT(*) FROM discovery_candidates dc WHERE dc.run_id=r.id AND dc.imported_paper_id<>'') AS imported_count
           FROM runs r LEFT JOIN run_snapshots rs ON rs.run_id=r.id
           ORDER BY r.started_at DESC LIMIT ?""",
        (min(max(int(limit), 1), 100),),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        summary = _services()._json_object(item.pop("candidate_summary_json", "{}"))
        item["query"] = _services().clean_text(
            item.pop("snapshot_query", ""), 1000
        ) or _services().clean_text(item.get("query_name", ""), 500)
        visible_ids = summary.get("candidate_result_ids")
        legacy_results = summary.get("candidate_results")
        stored_count = int(item.pop("candidate_row_count", 0) or 0)
        item["candidate_mode"] = bool(summary.get("candidate_mode"))
        item["candidate_count"] = (
            len(visible_ids)
            if isinstance(visible_ids, list)
            else (
                len(legacy_results)
                if isinstance(legacy_results, list)
                else stored_count
            )
        )
        item["imported_count"] = int(item.pop("imported_count", 0) or 0)
        item["awaiting_review_count"] = max(
            0, item["candidate_count"] - item["imported_count"]
        )
        result.append(item)
    return result


def _search_workflow_fields(search_status: str, triage: dict | None) -> dict:
    """Return one stable progress contract for discovery and AI screening.

    A discovery run has two durable phases: connector/model retrieval and the
    optional candidate-screening pass.  Older callers only received the run
    status and had to infer the second phase from a nested snapshot, which
    made a browser refresh look as if the job had disappeared.  Keep this
    helper pure so every HTTP/MCP-facing view uses exactly the same mapping.
    """
    search_status = _services().clean_text(search_status, 40).lower() or "unknown"
    triage = triage if isinstance(triage, dict) else {}
    screening_status = (
        _services().clean_text(triage.get("status"), 40).lower() or "not_requested"
    )

    def _count(*values) -> int:
        for value in values:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
        return 0

    processed = _count(triage.get("processed_count"), triage.get("screened"))
    total = _count(triage.get("candidate_count"), triage.get("total"))
    # A malformed provider response must never report impossible progress.
    processed = min(processed, total) if total else 0
    screening_progress = {
        "processed": processed,
        "total": total,
        "error": _services().clean_text(triage.get("error"), 1000),
    }

    search_terminal = search_status in {
        "success",
        "partial",
        "failed",
        "cancelled",
        "interrupted",
        "retryable",
    }
    screening_active = screening_status in {"queued", "running"}
    if search_terminal and screening_active:
        workflow_status = "screening_running"
    elif screening_status not in {"not_requested", ""} and not screening_active:
        # Preserve the existing triage vocabulary (completed/partial/failed)
        # so old clients can render it without a translation table.
        workflow_status = screening_status
    else:
        workflow_status = search_status
    return {
        "search_status": search_status,
        "screening_status": screening_status,
        "screening_progress": screening_progress,
        "workflow_status": workflow_status,
    }


def run_detail(run_id: str) -> dict | None:
    """Return an operator-safe, source-level explanation of one sync run."""
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        snapshot = db.execute(
            """SELECT rs.summary_json,
                      (SELECT COUNT(*) FROM discovery_candidates dc WHERE dc.run_id=rs.run_id) AS candidate_count,
                      (SELECT COUNT(*) FROM discovery_candidates dc WHERE dc.run_id=rs.run_id AND dc.imported_paper_id<>'') AS imported_count
               FROM run_snapshots rs WHERE rs.run_id=?""",
            (run_id,),
        ).fetchone()
        query = None
        if row["query_id"]:
            query = db.execute(
                "SELECT id,name,query,sources_json,search_options_json,cadence_hours,active FROM saved_queries WHERE id=?",
                (row["query_id"],),
            ).fetchone()
        job = db.execute(
            "SELECT id,status,stage,attempts,next_retry_at,last_error,created_at,updated_at FROM sync_jobs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    try:
        result["sources"] = _services().json.loads(result.pop("sources_json") or "[]")
    except (TypeError, _services().json.JSONDecodeError):
        result["sources"] = []
    try:
        raw_details = _services().json.loads(result.pop("source_details_json") or "[]")
    except (TypeError, _services().json.JSONDecodeError):
        raw_details = []
    result["source_details"] = []
    for detail in raw_details if isinstance(raw_details, list) else []:
        if not isinstance(detail, dict):
            continue
        item = {
            key: detail.get(key)
            for key in (
                "source",
                "status",
                "fetched",
                "accepted",
                "filtered",
                "variant_duplicates",
                "new",
                "duplicates",
                "error",
            )
        }
        item["variants"] = []
        for variant in detail.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            item["variants"].append(
                {
                    key: variant.get(key)
                    for key in (
                        "id",
                        "query",
                        "kind",
                        "fetched",
                        "accepted",
                        "filtered",
                    )
                }
            )
        result["source_details"].append(item)
    if query:
        query = dict(query)
        try:
            query["sources"] = _services().json.loads(query.pop("sources_json") or "[]")
        except (TypeError, _services().json.JSONDecodeError):
            query["sources"] = []
        try:
            query["search_options"] = _services().json.loads(
                query.pop("search_options_json") or "{}"
            )
        except (TypeError, _services().json.JSONDecodeError):
            query["search_options"] = {}
        result["query"] = query
    else:
        result["query"] = None
    result["data_quality"] = (
        "complete" if result["source_details"] else "historical_detail_missing"
    )
    job_snapshot = dict(job) if job else None
    result["job"] = job_snapshot
    # Expose the durable scheduler state at the run level as well.  The
    # discovery page refreshes both ``/api/runs/{id}`` and ``/results``; older
    # clients only looked at top-level fields and therefore lost the current
    # stage/source progress while a run was queued or running.  Keeping these
    # derived fields here makes a refreshed page immediately explain what is
    # happening without requiring a second task-center request.
    if job_snapshot:
        result["stage"] = job_snapshot.get("stage") or ""
        result["attempts"] = int(job_snapshot.get("attempts") or 0)
        result["next_retry_at"] = job_snapshot.get("next_retry_at") or ""
        result["last_error"] = job_snapshot.get("last_error") or ""
        source_names = result.get("sources") or []
        detail_sources = []
        for detail in result.get("source_details") or []:
            source_name = (
                _services().clean_text(detail.get("source"), 80)
                if isinstance(detail, dict)
                else ""
            )
            if source_name and source_name not in detail_sources:
                detail_sources.append(source_name)
        stage_hint = str(result["stage"] or "")
        if detail_sources and (not source_names or "model_web" in detail_sources):
            source_names = detail_sources
        elif not source_names and (
            stage_hint == "model_web_search"
            or stage_hint.startswith("model_web_search:")
        ):
            source_names = ["model_web"]
        result["sources"] = source_names
        stage = stage_hint
        if stage.startswith("model_web_search") and "model_web" in source_names:
            current_source = "model_web"
        else:
            current_source = (
                stage.split(":", 1)[1] if stage.startswith("source:") else ""
            )
        result["current_source"] = (
            current_source if current_source in source_names else ""
        )
        result["source_index"] = (
            source_names.index(current_source) + 1
            if current_source in source_names
            else (
                len(source_names)
                if result.get("status")
                in {"success", "partial", "failed", "interrupted"}
                else 0
            )
        )
        result["source_total"] = len(source_names)
        result["progress_current"] = result["source_index"]
        result["progress_total"] = result["source_total"]
    summary = _services()._json_object(snapshot["summary_json"] if snapshot else "{}")
    visible_ids = summary.get("candidate_result_ids")
    legacy_results = summary.get("candidate_results")
    result["candidate_mode"] = bool(summary.get("candidate_mode"))
    result["candidate_count"] = (
        len(visible_ids)
        if isinstance(visible_ids, list)
        else (
            len(legacy_results)
            if isinstance(legacy_results, list)
            else int(snapshot["candidate_count"] or 0) if snapshot else 0
        )
    )
    result["imported_count"] = int(snapshot["imported_count"] or 0) if snapshot else 0
    result["awaiting_review_count"] = max(
        0, result["candidate_count"] - result["imported_count"]
    )
    triage = _services().discovery_triage_state(run_id)
    result["triage"] = triage
    result["ai_screening"] = triage
    result.update(_services()._search_workflow_fields(result.get("status"), triage))
    return result


def run_results_snapshot(run_id: str) -> dict | None:
    """Return the durable result list for a search, including older-run status."""
    detail = _services().run_detail(run_id)
    if not detail:
        return None
    with _services().DB_LOCK, _services().db_session() as db:
        snapshot = db.execute(
            "SELECT query,plan_json,summary_json FROM run_snapshots WHERE run_id=?",
            (run_id,),
        ).fetchone()
        plan = _services()._json_object(snapshot["plan_json"] if snapshot else "{}")
        summary = _services()._json_object(
            snapshot["summary_json"] if snapshot else "{}"
        )
        rows = db.execute(
            """SELECT p.*,rp.search_rank AS result_rank,rp.metadata_json
               FROM run_papers rp JOIN papers p ON p.id=rp.paper_id
               WHERE rp.run_id=? ORDER BY rp.search_rank,p.title COLLATE NOCASE""",
            (run_id,),
        ).fetchall()
        papers = []
        for row in rows:
            paper = _services().paper_dict(row)
            paper["sources"] = [
                dict(source)
                for source in db.execute(
                    "SELECT source,external_id,fetched_at FROM source_records WHERE paper_id=? ORDER BY fetched_at DESC",
                    (paper["id"],),
                )
            ]
            try:
                metadata = _services().json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if isinstance(metadata, dict):
                paper.update(metadata)
            _services()._hydrate_retrieval_metadata(paper, "legacy_snapshot")
            paper["search_rank"] = int(
                row["result_rank"] or paper.get("search_rank") or 0
            )
            papers.append(paper)
        if not papers and summary.get("candidate_mode"):
            queue_rows = db.execute(
                "SELECT id,record_json,imported_paper_id,created_at FROM discovery_candidates WHERE run_id=? ORDER BY created_at,id",
                (run_id,),
            ).fetchall()
            intent_key = _services().clean_text(
                summary.get("intent_key") or plan.get("discovery_intent_key"), 100
            )
            if not intent_key:
                intent_key = _services().discovery_intent_key(
                    snapshot["query"] if snapshot else detail.get("query_name", ""),
                    plan.get("filters") if isinstance(plan, dict) else {},
                )
            feedback_by_key = _services()._discovery_feedback_by_key(db, intent_key)
            stored = {}
            for queue_row in queue_rows:
                item = _services()._json_object(queue_row["record_json"])
                if not item:
                    continue
                # Keep the public result payload bounded even for candidates
                # created by older versions that already contain nested raw
                # records.  The durable row is repaired on the next write.
                item = _services().compact_candidate_record(item)
                item.setdefault("id", queue_row["id"])
                _services()._hydrate_retrieval_metadata(item)
                item["candidate_key"] = _services().clean_text(
                    item.get("candidate_key"), 600
                ) or _services().candidate_identity(item)
                item["intent_key"] = (
                    _services().clean_text(item.get("intent_key"), 100) or intent_key
                )
                item["imported_paper_id"] = queue_row["imported_paper_id"] or ""
                prior = feedback_by_key.get(item["candidate_key"])
                if prior and prior.get("decision") != "new":
                    item["prior_feedback"] = dict(prior)
                    item["review_decision"] = prior["decision"]
                    item["user_decision"] = prior["decision"]
                else:
                    item.setdefault("prior_feedback", None)
                    item.setdefault(
                        "review_decision", item.get("user_decision", "new") or "new"
                    )
                stored[queue_row["id"]] = item
            visible_ids = summary.get("candidate_result_ids")
            if not isinstance(visible_ids, list):
                legacy = summary.get("candidate_results")
                visible_ids = (
                    [
                        item.get("id")
                        for item in legacy
                        if isinstance(item, dict) and item.get("id")
                    ]
                    if isinstance(legacy, list)
                    else None
                )
            # A candidate run is not ready for display until the ranked ID
            # list has been committed. Returning all staging rows while the
            # worker is still ranking causes a transient, much larger result
            # set that disappears on the next refresh. Legacy snapshots with
            # an embedded candidate list remain readable.
            if isinstance(visible_ids, list):
                papers = [
                    stored[candidate_id]
                    for candidate_id in visible_ids
                    if candidate_id in stored
                ]
            elif summary.get("candidate_results"):
                papers = list(stored.values())
            elif detail.get("status") in {
                "success",
                "partial",
                "failed",
                "interrupted",
            }:
                # A few early candidate runs persisted their staging rows but
                # predated ``candidate_result_ids``.  Once the run is
                # terminal, those rows are the only durable result set left;
                # restore them instead of showing an empty page after reload.
                # Running jobs deliberately stay empty until their final
                # ranked list is committed, so a refresh cannot expose
                # half-written candidates.
                papers = list(stored.values())
            else:
                papers = []
            papers.sort(
                key=lambda item: (
                    int(item.get("search_rank") or 999999),
                    item.get("id", ""),
                )
            )
            for paper in papers:
                if str(paper.get("id", "")).startswith("candidate:"):
                    match = _services().find_existing_paper(db, paper)
                    paper["in_library_paper_id"] = match["id"] if match else ""
                else:
                    paper["in_library_paper_id"] = paper.get("id", "")
    public_summary = dict(summary)
    public_summary.pop("candidate_results", None)
    public_summary.pop("candidate_result_ids", None)
    details = detail.get("source_details") or []
    triage = _services().discovery_triage_state(run_id)
    result_status = detail.get("status") or "unknown"
    if (
        summary.get("candidate_mode")
        and not papers
        and not isinstance(summary.get("candidate_result_ids"), list)
        and result_status in {"success", "partial"}
    ):
        result_status = "running"
    workflow_fields = _services()._search_workflow_fields(result_status, triage)
    return {
        "id": run_id,
        "query": snapshot["query"] if snapshot else (detail.get("query_name") or ""),
        "query_name": detail.get("query_name") or "",
        "status": result_status,
        "new_count": int(detail.get("new_count") or 0),
        "duplicate_count": int(detail.get("duplicate_count") or 0),
        "error": detail.get("error") or "",
        "sources": details,
        "source_names": detail.get("sources") or [],
        "total_fetched": sum(
            int(item.get("fetched") or 0) for item in details if isinstance(item, dict)
        ),
        "total_filtered": sum(
            int(item.get("filtered") or 0) for item in details if isinstance(item, dict)
        ),
        "papers": papers,
        "plan": plan,
        "summary": public_summary,
        "accepted_count": len(papers),
        "candidate_mode": bool(summary.get("candidate_mode")),
        "intent_key": public_summary.get("intent_key")
        or plan.get("discovery_intent_key", ""),
        # Mirror the durable run progress so a client that only polls
        # ``/results`` (including a restored browser tab) does not need to
        # reconstruct it from the nested legacy ``run`` object.
        "stage": detail.get("stage", ""),
        "attempts": int(detail.get("attempts") or 0),
        "next_retry_at": detail.get("next_retry_at", ""),
        "last_error": detail.get("last_error", ""),
        "current_source": detail.get("current_source", ""),
        "source_index": int(detail.get("source_index") or 0),
        "source_total": int(detail.get("source_total") or 0),
        "progress_current": int(detail.get("progress_current") or 0),
        "progress_total": int(detail.get("progress_total") or 0),
        "triage": triage,
        "ai_screening": triage,
        **workflow_fields,
        "results_available": bool(snapshot),
        "run": detail,
    }


def discovery_history(limit: int = 20, offset: int = 0, query: str = "") -> list[dict]:
    """Return compact, durable history for the Discovery workspace."""
    limit = min(max(int(limit), 1), 100)
    offset = max(int(offset), 0)
    query = _services().clean_text(query, 300)
    with _services().DB_LOCK, _services().db_session() as db:
        candidate_mode = (
            "COALESCE((rs.summary_json::jsonb ->> 'candidate_mode')::boolean,FALSE)"
            if _services().using_postgres()
            else "json_extract(CASE WHEN json_valid(rs.summary_json) THEN rs.summary_json ELSE '{}' END,'$.candidate_mode')=1"
        )
        query_match = (
            "strpos(lower(COALESCE(rs.query,r.query_name,'')),lower(?))>0"
            if _services().using_postgres()
            else "instr(lower(COALESCE(rs.query,r.query_name,'')),lower(?))>0"
        )
        rows = db.execute(
            f"""SELECT r.id,r.query_name,r.status,r.started_at,r.completed_at,r.error,
                      r.sources_json,rs.query,rs.plan_json,rs.summary_json,
                      (SELECT COUNT(*) FROM discovery_candidates dc WHERE dc.run_id=r.id) AS candidate_count,
                      (SELECT COUNT(*) FROM discovery_candidates dc WHERE dc.run_id=r.id AND dc.imported_paper_id<>'') AS imported_count
               FROM runs r LEFT JOIN run_snapshots rs ON rs.run_id=r.id
               WHERE (EXISTS(SELECT 1 FROM discovery_candidates dc WHERE dc.run_id=r.id)
                  OR {candidate_mode})
                  AND (?='' OR {query_match})
               ORDER BY r.started_at DESC,r.id DESC LIMIT ? OFFSET ?""",
            (query, query, limit, offset),
        ).fetchall()
    history = []
    for row in rows:
        summary = _services()._json_object(row["summary_json"])
        if not summary.get("candidate_mode") and not row["candidate_count"]:
            continue
        candidates = (
            summary.get("candidate_results")
            if isinstance(summary.get("candidate_results"), list)
            else []
        )
        candidate_count = int(row["candidate_count"] or 0) or len(candidates)
        try:
            plan = _services().json.loads(row["plan_json"] or "{}")
        except (TypeError, ValueError):
            plan = {}
        try:
            sources = _services().json.loads(row["sources_json"] or "[]")
        except (TypeError, ValueError):
            sources = []
        triage = _services().discovery_triage_state(row["id"])
        history.append(
            {
                "id": row["id"],
                "query": row["query"] or row["query_name"] or "",
                "query_name": row["query_name"] or "",
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"] or "",
                "error": row["error"] or "",
                "candidate_mode": True,
                "sources": sources if isinstance(sources, list) else [],
                "result_count": (
                    len(summary["candidate_result_ids"])
                    if isinstance(summary.get("candidate_result_ids"), list)
                    else candidate_count
                ),
                "candidate_count": candidate_count,
                "imported_count": int(row["imported_count"] or 0),
                "intent_key": summary.get("intent_key")
                or (
                    plan.get("discovery_intent_key", "")
                    if isinstance(plan, dict)
                    else ""
                ),
                "triage": triage,
                "ai_screening": triage,
                **_services()._search_workflow_fields(row["status"], triage),
                "plan": plan if isinstance(plan, dict) else {},
                "summary": {
                    key: value
                    for key, value in summary.items()
                    if key not in {"candidate_results", "candidate_result_ids"}
                },
            }
        )
        if len(history) >= limit:
            break
    return history


__all__ = [
    "evaluation_summary",
    "analytics_summary",
    "health_summary",
    "diagnostics_summary",
    "run_list_snapshots",
    "_search_workflow_fields",
    "run_detail",
    "run_results_snapshot",
    "discovery_history",
]
