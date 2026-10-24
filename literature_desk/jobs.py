"""Durable background workers, scheduling, and AI job lifecycle."""

from __future__ import annotations
import threading

from .services import app_services as _services


def _register_background_thread(thread: threading.Thread) -> None:
    """Track daemon workers so a controlled stop can give them a chance to finish."""
    with _services().BACKGROUND_THREADS_LOCK:
        _services().BACKGROUND_THREADS.add(thread)


def _unregister_background_thread(thread: threading.Thread | None = None) -> None:
    current = thread or _services().threading.current_thread()
    with _services().BACKGROUND_THREADS_LOCK:
        _services().BACKGROUND_THREADS.discard(current)


def _current_user_scope_key() -> str:
    actor = _services().AUTH_ACTOR.get() or {}
    user_id = _services().clean_text(actor.get("user_id", ""), 120)
    workspace_id = _services().clean_text(actor.get("workspace_id", ""), 120)
    return f"{workspace_id}:{user_id}" if user_id else ""


def shutdown_background_workers(wait: bool = True) -> None:
    """Stop durable workers without leaving rows permanently queued.

    The web server can be restarted while a model or PDF task is running.
    Pending work is marked for startup recovery, queued executor futures are
    cancelled, and active top-level workers are allowed to finish before the
    interpreter begins its own executor shutdown hook.  This ordering avoids
    the ``cannot schedule new futures after interpreter shutdown`` race that
    occurred when a nested PDF/visual pool was created during process exit.
    """
    _services().STOP_EVENT.set()
    try:
        with _services().DB_LOCK, _services().db_session() as db:
            now = _services().now_iso()
            db.execute(
                "UPDATE ai_jobs SET status='interrupted',stage='interrupted',message=?,error='服务重启时任务未完成',updated_at=?,completed_at=? WHERE status='queued'",
                (_services().RUNTIME_SHUTDOWN_MESSAGE, now, now),
            )
            db.execute(
                "UPDATE metadata_draft_jobs SET status='interrupted',stage='interrupted',message=?,error='服务重启时任务未完成',updated_at=?,completed_at=? WHERE status='queued'",
                (_services().RUNTIME_SHUTDOWN_MESSAGE, now, now),
            )
            db.execute(
                "UPDATE pdf_ingest_jobs SET status='interrupted',stage='interrupted',error='服务重启时任务未完成',updated_at=?,completed_at=? WHERE status='queued'",
                (now, now),
            )
            db.execute(
                "UPDATE semantic_index_jobs SET status='interrupted',stage='interrupted',message=?,error='服务重启时任务未完成',updated_at=?,completed_at=? WHERE status IN ('queued','running')",
                (_services().RUNTIME_SHUTDOWN_MESSAGE, now, now),
            )
            db.execute(
                "UPDATE agent_runs SET status='interrupted',error='服务重启时任务未完成',updated_at=? WHERE status='queued'",
                (now,),
            )
            db.execute(
                "UPDATE agent_steps SET status='queued',error='服务重启时任务未完成',updated_at=? WHERE status='running' AND run_id IN (SELECT id FROM agent_runs WHERE status='interrupted')",
                (now,),
            )
    except Exception:
        # Teardown must continue even when a database connection is already
        # unavailable; startup reconciliation remains the final safety net.
        pass

    # Agent runs can call the tool executor.  Shut down the parent pool first
    # while the tool pool is still available, then close the tool pool.
    executor_specs = [
        (
            "agent-run",
            "AGENT_RUN_EXECUTOR",
            _services().AGENT_RUN_EXECUTOR,
            _services().AGENT_RUN_EXECUTOR_LOCK,
        ),
        (
            "ai-job",
            "AI_JOB_EXECUTOR",
            _services().AI_JOB_EXECUTOR,
            _services().AI_JOB_EXECUTOR_LOCK,
        ),
        (
            "metadata-draft",
            "METADATA_DRAFT_EXECUTOR",
            _services().METADATA_DRAFT_EXECUTOR,
            _services().METADATA_DRAFT_EXECUTOR_LOCK,
        ),
        (
            "pdf-ingest",
            "PDF_INGEST_EXECUTOR",
            _services().PDF_INGEST_EXECUTOR,
            _services().PDF_INGEST_EXECUTOR_LOCK,
        ),
        (
            "semantic-index",
            "SEMANTIC_INDEX_EXECUTOR",
            _services().SEMANTIC_INDEX_EXECUTOR,
            _services().SEMANTIC_INDEX_EXECUTOR_LOCK,
        ),
        (
            "agent-tool",
            "AGENT_TOOL_EXECUTOR",
            _services().AGENT_TOOL_EXECUTOR,
            _services().AGENT_TOOL_EXECUTOR_LOCK,
        ),
        (
            "openalex-recommendation",
            "OPENALEX_RECOMMENDATION_EXECUTOR",
            _services().OPENALEX_RECOMMENDATION_EXECUTOR,
            _services().OPENALEX_RECOMMENDATION_EXECUTOR_LOCK,
        ),
    ]
    for _name, attr, current_executor, lock in executor_specs:
        with lock:
            executor = current_executor
            if attr == "AGENT_RUN_EXECUTOR":
                _services().AGENT_RUN_EXECUTOR = None
            elif attr == "AI_JOB_EXECUTOR":
                _services().AI_JOB_EXECUTOR = None
            elif attr == "METADATA_DRAFT_EXECUTOR":
                _services().METADATA_DRAFT_EXECUTOR = None
            elif attr == "PDF_INGEST_EXECUTOR":
                _services().PDF_INGEST_EXECUTOR = None
            elif attr == "SEMANTIC_INDEX_EXECUTOR":
                _services().SEMANTIC_INDEX_EXECUTOR = None
            elif attr == "AGENT_TOOL_EXECUTOR":
                _services().AGENT_TOOL_EXECUTOR = None
            elif attr == "OPENALEX_RECOMMENDATION_EXECUTOR":
                _services().OPENALEX_RECOMMENDATION_EXECUTOR = None
        if executor is None:
            continue
        try:
            executor.shutdown(wait=wait, cancel_futures=True)
        except TypeError:
            # Python 3.8 compatibility for environments without
            # ``cancel_futures``.
            executor.shutdown(wait=wait)
        except RuntimeError:
            # An interpreter-level shutdown can race this best-effort cleanup;
            # all durable rows have already been marked for recovery above.
            continue

    scoped_executors = []
    for attr, lock_attr in (
        ("USER_AGENT_RUN_EXECUTORS", "USER_AGENT_RUN_EXECUTORS_LOCK"),
        ("USER_AI_JOB_EXECUTORS", "USER_AI_JOB_EXECUTORS_LOCK"),
    ):
        with getattr(_services(), lock_attr):
            mapping = getattr(_services(), attr)
            scoped_executors.extend(mapping.values())
            mapping.clear()
    for executor in scoped_executors:
        try:
            executor.shutdown(wait=wait, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=wait)
        except RuntimeError:
            continue

    if wait:
        # Search and discovery workers use short-lived local pools rather than
        # a shared executor. Give them a bounded grace period so normal Ctrl+C
        # exits are quiet without making a stuck provider block shutdown
        # forever. Their rows are reconciled on the next startup if needed.
        deadline = _services().time.monotonic() + 5.0
        while _services().time.monotonic() < deadline:
            with _services().BACKGROUND_THREADS_LOCK:
                live = [
                    thread
                    for thread in _services().BACKGROUND_THREADS
                    if thread is not _services().threading.current_thread()
                    and thread.is_alive()
                ]
            if not live:
                break
            for thread in live:
                thread.join(
                    timeout=max(
                        0.05, min(0.25, deadline - _services().time.monotonic())
                    )
                )


def scheduled_workspace_tick(actor: dict | None, due: str) -> None:
    """Run one durable scheduler pass inside one workspace context."""
    token = _services().AUTH_ACTOR.set(actor)
    try:
        _services().scheduled_backup_tick()
        with _services().DB_LOCK, _services().db_session() as db:
            folder_rows = db.execute(
                "SELECT id FROM paper_folder_sources WHERE enabled=1 AND (next_scan_at='' OR next_scan_at<=?) ORDER BY next_scan_at LIMIT 1",
                (due,),
            ).fetchall()
        for folder_row in folder_rows:
            try:
                _services().scan_paper_folder(folder_row["id"])
            except Exception as exc:
                with _services().DB_LOCK, _services().db_session() as db:
                    db.execute(
                        "UPDATE paper_folder_sources SET last_error=?,next_scan_at=?,updated_at=? WHERE id=?",
                        (
                            str(exc)[:500],
                            (
                                _services().datetime.now(_services().timezone.utc)
                                + _services().timedelta(minutes=15)
                            )
                            .replace(microsecond=0)
                            .isoformat(),
                            _services().now_iso(),
                            folder_row["id"],
                        ),
                    )
        with _services().DB_LOCK, _services().db_session() as db:
            rows = db.execute(
                "SELECT * FROM saved_queries WHERE active=1 AND (next_run_at='' OR next_run_at<=?) ORDER BY next_run_at LIMIT 3",
                (due,),
            ).fetchall()
        for row in rows:
            try:
                options = _services().json.loads(row["search_options_json"] or "{}")
            except (_services().json.JSONDecodeError, TypeError):
                options = {}
            try:
                _services().run_query(
                    row["query"],
                    _services().json.loads(row["sources_json"]),
                    20,
                    row["id"],
                    True,
                    row["name"],
                    _services().prepare_user_search_options(options),
                )
            except Exception as exc:
                with _services().DB_LOCK, _services().db_session() as db:
                    db.execute(
                        "UPDATE saved_queries SET next_run_at=?,updated_at=? WHERE id=?",
                        (
                            (
                                _services().datetime.now(_services().timezone.utc)
                                + _services().timedelta(hours=1)
                            )
                            .replace(microsecond=0)
                            .isoformat(),
                            _services().now_iso(),
                            row["id"],
                        ),
                    )
                    db.execute(
                        "INSERT INTO runs(id,query_id,query_name,status,started_at,completed_at,error,is_scheduled) VALUES(?,?,?,?,?,?,?,1)",
                        (
                            str(_services().uuid.uuid4()),
                            row["id"],
                            row["name"],
                            "failed",
                            _services().now_iso(),
                            _services().now_iso(),
                            str(exc)[:1000],
                        ),
                    )
        with _services().DB_LOCK, _services().db_session() as db:
            goal_rows = db.execute(
                "SELECT * FROM research_goals WHERE status='active' AND auto_run=1 AND (next_run_at='' OR next_run_at<=?) ORDER BY next_run_at LIMIT 2",
                (due,),
            ).fetchall()
        for goal_row in goal_rows:
            try:
                with _services().DB_LOCK, _services().db_session() as db:
                    running = db.execute(
                        "SELECT 1 FROM agent_runs WHERE goal_id=? AND status IN ('queued','running','awaiting_confirmation','paused') LIMIT 1",
                        (goal_row["id"],),
                    ).fetchone()
                if running:
                    continue
                run = _services().create_agent_run(goal_row["id"], "scheduled")
                _services().approve_agent_run(run["id"], consent_external=False)
                cadence = min(max(int(goal_row["cadence_hours"] or 168), 24), 8760)
                next_run = (
                    (
                        _services().datetime.now(_services().timezone.utc)
                        + _services().timedelta(hours=cadence)
                    )
                    .replace(microsecond=0)
                    .isoformat()
                )
                with _services().DB_LOCK, _services().db_session() as db:
                    db.execute(
                        "UPDATE research_goals SET next_run_at=?,updated_at=? WHERE id=?",
                        (next_run, _services().now_iso(), goal_row["id"]),
                    )
                    _services().record_audit(
                        db,
                        "agent.run_scheduled",
                        "research_goal",
                        goal_row["id"],
                        {"run_id": run["id"], "next_run_at": next_run},
                    )
            except Exception as exc:
                with _services().DB_LOCK, _services().db_session() as db:
                    db.execute(
                        "UPDATE research_goals SET next_run_at=?,updated_at=? WHERE id=?",
                        (
                            (
                                _services().datetime.now(_services().timezone.utc)
                                + _services().timedelta(hours=1)
                            )
                            .replace(microsecond=0)
                            .isoformat(),
                            _services().now_iso(),
                            goal_row["id"],
                        ),
                    )
                    _services().record_audit(
                        db,
                        "agent.schedule_failed",
                        "research_goal",
                        goal_row["id"],
                        {"error": _services().clean_text(str(exc), 500)},
                    )
    finally:
        _services().AUTH_ACTOR.reset(token)


def scheduler_loop():
    # Persistent due times live in SQLite; if the app was closed, due queries run after restart.
    _services().SCHEDULER_LAST_TICK = _services().now_iso()
    while not _services().STOP_EVENT.is_set():
        try:
            _services().SCHEDULER_LAST_TICK = _services().now_iso()
            due = (
                _services()
                .datetime.now(_services().timezone.utc)
                .replace(microsecond=0)
                .isoformat()
            )
            actors = _services().isolated_workspace_actors()
            for actor in actors:
                try:
                    _services().scheduled_workspace_tick(actor, due)
                except Exception:
                    # Keep one broken tenant from starving all other workspaces.
                    continue
        except Exception:
            # Keep the local app alive if the database or a connector has a transient failure.
            _services().time.sleep(2)
        if _services().STOP_EVENT.wait(45):
            break


class AIJobCancelled(Exception):
    """Raised inside a background AI job when the user requests cancellation."""


def ai_job_snapshot(row, include_result: bool = False) -> dict:
    if not row:
        return {}
    item = dict(row)
    try:
        item["options"] = _services().json.loads(item.pop("options_json", "{}") or "{}")
    except (TypeError, ValueError):
        item["options"] = {}
    item["cancel_requested"] = bool(item.get("cancel_requested"))
    stored_duration = float(item.get("duration_ms") or 0)
    if stored_duration > 0:
        item["duration_ms"] = round(stored_duration, 2)
    else:
        started = item.get("started_at") or item.get("created_at")
        end = item.get("completed_at") or _services().now_iso()
        try:
            item["duration_ms"] = max(
                0,
                round(
                    (
                        _services().datetime.fromisoformat(end)
                        - _services().datetime.fromisoformat(started)
                    ).total_seconds()
                    * 1000,
                    2,
                ),
            )
        except (TypeError, ValueError):
            item["duration_ms"] = 0
    if (
        include_result
        and item.get("status") in {"success", "partial"}
        and item.get("artifact_id")
    ):
        with _services().DB_LOCK, _services().db_session() as db:
            artifact = db.execute(
                "SELECT artifact_type,model,answer FROM ai_artifacts WHERE id=?",
                (item["artifact_id"],),
            ).fetchone()
        if artifact:
            if artifact["artifact_type"] == "abstract_brief":
                fallback = _services().ABSTRACT_LOCAL_FALLBACK_MARKER in (
                    artifact["answer"] or ""
                )
                item["result"] = {
                    "answer": artifact["answer"],
                    "basis": "标题、摘要和元数据；非全文证据",
                    "model": artifact["model"],
                    "fallback": fallback,
                }
                item["answer"] = artifact["answer"]
                item["model"] = artifact["model"]
                item["fallback"] = fallback
            else:
                try:
                    payload = _services().json.loads(artifact["answer"])
                    item["result"] = payload.get("analysis", payload)
                    item["meta"] = payload.get("meta", {})
                except (TypeError, ValueError):
                    item["result"] = {}
    (
        item.pop("cancel_requested", None)
        if item.get("status") not in {"queued", "running"}
        else None
    )
    return item


def update_ai_job(job_id: str, **updates) -> dict:
    allowed = {
        "status",
        "stage",
        "progress_current",
        "progress_total",
        "message",
        "artifact_id",
        "error",
        "started_at",
        "updated_at",
        "completed_at",
        "cancel_requested",
        "duration_ms",
        "options_json",
    }
    values = {key: value for key, value in updates.items() if key in allowed}
    values.setdefault("updated_at", _services().now_iso())
    if not values:
        return _services().get_ai_job(job_id) or {}
    assignments = ",".join(f"{key}=?" for key in values)
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            f"UPDATE ai_jobs SET {assignments} WHERE id=?", (*values.values(), job_id)
        )
        row = db.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    return _services().ai_job_snapshot(row) if row else {}


def get_ai_job(job_id: str, include_result: bool = False) -> dict | None:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    return _services().ai_job_snapshot(row, include_result) if row else None


def _partial_deep_read_artifact_id(paper_id: str, artifact_id: str) -> str:
    """Return an artifact only when it contains resumable partial evidence."""
    artifact_id = _services().clean_text(artifact_id, 100)
    if not artifact_id:
        return ""
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT answer FROM ai_artifacts WHERE id=? AND paper_id=? AND artifact_type='deep_read'",
            (artifact_id, paper_id),
        ).fetchone()
    if not row:
        return ""
    try:
        payload = _services().json.loads(row["answer"] or "{}")
    except (TypeError, ValueError, _services().json.JSONDecodeError):
        return ""
    meta = (
        payload.get("meta")
        if isinstance(payload, dict) and isinstance(payload.get("meta"), dict)
        else {}
    )
    if (
        meta.get("analysis_status") != "partial"
        or meta.get("synthesis_retry_available") is False
    ):
        return ""
    return artifact_id


def run_deep_read_synthesis_retry(
    paper_id: str, artifact_id: str, progress_callback=None
) -> dict:
    """Rebuild the cross-page synthesis from saved, page-verified chunk findings only."""
    started = _services().time.perf_counter()
    with _services().DB_LOCK, _services().db_session() as db:
        source_row = db.execute(
            "SELECT answer,input_sha256 FROM ai_artifacts WHERE id=? AND paper_id=? AND artifact_type='deep_read'",
            (artifact_id, paper_id),
        ).fetchone()
    if not source_row:
        raise LookupError("找不到可重试综合的精读证据")
    try:
        saved = _services().json.loads(source_row["answer"] or "{}")
    except (TypeError, ValueError, _services().json.JSONDecodeError) as exc:
        raise ValueError("已保存的精读证据格式损坏，无法重试综合") from exc
    source_analysis = (
        saved.get("analysis") if isinstance(saved.get("analysis"), dict) else {}
    )
    source_meta = saved.get("meta") if isinstance(saved.get("meta"), dict) else {}
    source_findings = (
        source_analysis.get("partial_findings")
        if isinstance(source_analysis.get("partial_findings"), list)
        else []
    )
    notes = []
    text_findings = []
    visual_findings = (
        source_analysis.get("visual_evidence")
        if isinstance(source_analysis.get("visual_evidence"), list)
        else []
    )
    for item in source_findings:
        if not isinstance(item, dict) or not _services()._meaningful_model_value(
            item.get("finding")
        ):
            continue
        spans = (
            [
                dict(span)
                for span in item.get("evidence_spans", [])
                if isinstance(span, dict)
            ]
            if isinstance(item.get("evidence_spans"), list)
            else []
        )
        note = {
            "category": _services().clean_text(item.get("category", ""), 80),
            "finding": _services().clean_text(item.get("finding", ""), 1000),
            "evidence_spans": spans,
        }
        text_findings.append(note)
    if text_findings:
        notes.append(
            {"notes": text_findings, "evidence_spans": [], "visual_observations": []}
        )
    if visual_findings:
        notes.append(
            {
                "notes": [],
                "evidence_spans": [],
                "visual_observations": [
                    dict(item) for item in visual_findings if isinstance(item, dict)
                ],
            }
        )
    compact_context = _services().compact_deep_read_repair_context(notes)
    if not compact_context:
        raise ValueError("已保存结果里没有可重试的分块文字或视觉发现")
    paper, rows = _services().get_paper_pages_for_ai(paper_id)
    if progress_callback:
        progress_callback(
            "synthesis_retry",
            0,
            1,
            f"已载入 {len(text_findings)} 条分块发现和 {len(visual_findings)} 条视觉观察，仅重新综合证据",
        )
    prompt = (
        "你是严谨的研究生论文精读综合器。输入是此前从 PDF 分块识别并保存的发现及其证据。"
        "只根据输入生成一份结构化跨页精读，不要要求重新读取 PDF，不要输出 Markdown、解释文字或思考过程。"
        "顶层必须包含 overview、research_question、motivation、contributions、problem_formulation、method、equation_analysis、datasets、experiments、result_tables、metrics、main_findings、limitations、reproducibility、critical_reading、visual_analysis、visual_evidence、evidence_spans、uncertainties、reading_card。"
        "overview、research_question、main_findings 和 (method.pipeline 或 equation_analysis) 必须有实质内容；不能只返回单条发现、单条引文或 visual_evidence。"
        "main_findings 总结最重要的输入发现；method.pipeline 仅在输入发现足以支持时复原方法，否则清楚说明证据不足。所有结论只来自输入发现，推断明确标为‘AI推断’，不补写没有给出的实验数值、数据集、基线、公式或因果结论。"
        "保留 evidence_spans 的页码和逐字引文，不得改写引文。可复用 visual_evidence，但不能增加未在输入里出现的页面观察。空字符串只表示可选字段无内容，不得把字段名或提示语复制成答案。"
        '使用 JSON 对象，顶层结构示例：{"overview":"","research_question":"","method":{"pipeline":""},"main_findings":[],"limitations":[],"evidence_spans":[],"visual_evidence":[],"uncertainties":[]}。示例中的空值不是答案；必须填写有证据支持的核心内容。'
    )
    payload = {
        "paper": {
            key: paper.get(key, "")
            for key in ("title", "authors", "year", "venue", "topic")
        },
        "evidence_chunks": compact_context,
    }
    call_metrics, call_lock = [], _services().threading.Lock()
    rendered_visual_pages = {
        (
            _services().clean_text(item.get("attachment_id", ""), 100),
            int(item.get("page") or 0),
        )
        for item in source_meta.get("visual_page_refs", [])
        if isinstance(item, dict) and item.get("attachment_id") and item.get("page")
    }
    synthesis_failure = ""
    try:
        result = _services().record_timed_model_call(
            call_metrics,
            call_lock,
            "synthesis_retry",
            "text",
            lambda: _services().limited_text_model_request(
                prompt,
                payload,
                timeout=90,
                output_tokens=_services().MODEL_SYNTHESIS_MAX_OUTPUT_TOKENS,
                json_contract=_services().DEEP_READ_SYNTHESIS_JSON_CONTRACT,
            ),
            len(_services().json.dumps(payload, ensure_ascii=False)),
        )
        result = _services().unwrap_model_json_layers(result)
        if isinstance(result.get("analysis"), dict):
            result = result["analysis"]
        if not isinstance(result, dict):
            raise ValueError("精读综合响应不是结构化 JSON")
        result.setdefault(
            "prereading_guide", source_analysis.get("prereading_guide", {})
        )
        prior_uncertainties = (
            source_analysis.get("uncertainties")
            if isinstance(source_analysis.get("uncertainties"), list)
            else []
        )
        result_uncertainties = (
            result.get("uncertainties")
            if isinstance(result.get("uncertainties"), list)
            else []
        )
        result["uncertainties"] = list(
            dict.fromkeys([*prior_uncertainties, *result_uncertainties])
        )
        result["prereading_guide"] = _services().normalize_prereading_guide(
            result.get("prereading_guide")
        )
        _services().merge_chunk_visual_evidence(result, notes)
        _services().merge_chunk_text_evidence(result, notes)
        _services().validate_ai_evidence(result, rows, rendered_visual_pages)
        _services().validate_fulltext_analysis("deep_read", result)
    except Exception as exc:
        synthesis_failure = (
            _services().clean_text(str(exc), 600) or "模型没有生成可用的跨页综合"
        )
        result = _services().verified_partial_deep_read(
            notes,
            rows,
            rendered_visual_pages,
            f"跨页综合重试未完成：{synthesis_failure}",
        )
        if not result:
            raise ValueError(
                f"仅重试综合失败且没有可核验的部分结果：{synthesis_failure}"
            ) from exc
        result["prereading_guide"] = _services().normalize_prereading_guide(
            source_analysis.get("prereading_guide", {})
        )
        _services().validate_fulltext_analysis("deep_read", result)
    analysis_status = (
        "partial" if result.get("analysis_status") == "partial" else "complete"
    )
    duration_ms = round((_services().time.perf_counter() - started) * 1000, 2)
    settings = _services().resolve_model_route(_services().model_settings(), "analysis")
    created = _services().now_iso()
    source_input_hash = _services().clean_text(
        source_meta.get("input_sha256", "") or source_row["input_sha256"], 100
    )
    if not source_input_hash:
        source_input_hash = (
            _services().clean_text(saved.get("input_sha256", ""), 100) or artifact_id
        )
    input_hash = (
        _services()
        .hashlib.sha256(
            f"{source_input_hash}:deep-read-synthesis-retry-v2".encode("utf-8")
        )
        .hexdigest()
    )
    metrics = call_metrics
    meta = {
        **source_meta,
        "page_count": len(rows),
        "characters": sum(int(row.get("char_count") or 0) for row in rows),
        "duration_ms": duration_ms,
        "total_duration_ms": round(
            float(source_meta.get("duration_ms") or 0) + duration_ms, 2
        ),
        "model_call_count": len(metrics),
        "model_call_total_ms": round(
            sum(float(item.get("duration_ms") or 0) for item in metrics), 2
        ),
        "model_call_average_ms": (
            round(
                sum(float(item.get("duration_ms") or 0) for item in metrics)
                / len(metrics),
                2,
            )
            if metrics
            else 0
        ),
        "model_call_max_ms": round(
            max((float(item.get("duration_ms") or 0) for item in metrics), default=0), 2
        ),
        "model_calls": metrics,
        "model_identity": {
            "model": settings.get("model", ""),
            "protocol": settings.get("protocol", ""),
            "endpoint": _services().model_endpoint(settings),
            **(
                {
                    "vision_model": _services().clean_text(
                        (source_meta.get("model_identity") or {}).get(
                            "vision_model", ""
                        ),
                        200,
                    )
                }
                if (source_meta.get("model_identity") or {}).get("vision_model")
                else {}
            ),
        },
        "pipeline_version": _services().DEEP_READ_PIPELINE_VERSION,
        "analysis_status": analysis_status,
        "synthesis_retry": True,
        "resumed_from_artifact_id": artifact_id,
        "synthesis_repair_attempted": True,
        "synthesis_repair_succeeded": analysis_status == "complete",
        "synthesis_repair_error": synthesis_failure,
    }
    new_artifact_id = str(_services().uuid.uuid4())
    answer = _services().json.dumps(
        {"analysis": result, "meta": meta}, ensure_ascii=False
    )
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                new_artifact_id,
                paper_id,
                "deep_read",
                settings["base_url"],
                settings["model"],
                "deep-read-synthesis-retry-v2",
                input_hash,
                answer,
                created,
            ),
        )
        _services().sync_prereading_study_items(
            db, paper_id, result.get("prereading_guide"), new_artifact_id, created
        )
        _services().record_audit(
            db,
            "ai.fulltext_analysis.synthesis_retried",
            "paper",
            paper_id,
            {"artifact_id": new_artifact_id, "source_artifact_id": artifact_id, **meta},
        )
    if progress_callback:
        message = (
            "已使用已保存证据完成跨页综合"
            if analysis_status == "complete"
            else "综合未完成；已保存可核验的分块结果，可再次重试"
        )
        progress_callback("saving", 1, 1, message)
    return {
        "analysis": result,
        "artifact_id": new_artifact_id,
        "paper_id": paper_id,
        "model": settings["model"],
        "prompt_version": "deep-read-synthesis-retry-v2",
        "input_sha256": input_hash,
        "created_at": created,
        "analysis_status": analysis_status,
        "chunk_count": int(source_meta.get("chunk_count") or 0),
        **meta,
    }


def _ai_job_is_cancelled(job_id: str) -> bool:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT cancel_requested,status FROM ai_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return bool(row and (row[0] or row[1] == "cancelled"))


def _is_interpreter_executor_shutdown(exc: BaseException) -> bool:
    """Recognize a process-teardown race separately from a model failure."""
    return (
        isinstance(exc, RuntimeError)
        and "cannot schedule new futures after" in str(exc).casefold()
    )


def run_ai_job(job_id: str) -> None:
    job = _services().get_ai_job(job_id)
    if not job or job.get("status") != "queued":
        return
    if _services()._ai_job_is_cancelled(job_id):
        _services().update_ai_job(
            job_id,
            status="cancelled",
            stage="cancelled",
            message="任务已取消",
            completed_at=_services().now_iso(),
        )
        return
    job_started = _services().time.perf_counter()
    options = job.get("options") or {}
    job_type = job.get("job_type")
    synthesis_source = _services().clean_text(
        options.get("synthesis_only_artifact_id", ""), 100
    )
    started_at = _services().now_iso()
    is_abstract_brief = job_type == "abstract_brief"
    is_paragraph_explain = job_type == "paragraph_explain"
    is_reading_segmentation = job_type == "reading_segmentation"
    _services().update_ai_job(
        job_id,
        status="running",
        stage=(
            "synthesis_retry"
            if synthesis_source
            else "model_request"
            if is_abstract_brief or is_paragraph_explain or is_reading_segmentation
            else "extracting"
        ),
        started_at=started_at,
        progress_current=0,
        progress_total=1
        if synthesis_source or is_abstract_brief or is_paragraph_explain or is_reading_segmentation
        else 0,
        message=(
            "正在载入已保存证据并准备综合"
            if synthesis_source
            else (
                "正在整理论文摘要与元数据"
                if is_abstract_brief
                else "正在解释当前原文段落"
                if is_paragraph_explain
                else "正在判断阅读块边界"
                if is_reading_segmentation
                else "正在准备 PDF 页面和文本"
            )
        ),
    )

    # A deep-read job has several phases that reuse the same progress row.
    # Visual recheck reports its own local counter (0..N), and synthesis then
    # reports another counter (N..total); writing those raw values made a
    # running job appear to move backwards after a refresh. Keep one monotonic
    # job-level counter while the stage/message still explains the active work.
    progress_lock = _services().threading.Lock()
    last_progress_current = 0
    last_progress_total = 0

    def progress(stage, current, total, message):
        nonlocal last_progress_current, last_progress_total
        if _services()._ai_job_is_cancelled(job_id):
            raise _services().AIJobCancelled("任务已取消")
        with progress_lock:
            reported_total = max(int(total or 0), last_progress_total)
            if current is not None:
                last_progress_current = max(last_progress_current, int(current or 0))
            last_progress_total = reported_total
            # Never expose a value above the current total if a phase supplies
            # a smaller local total than the previous phase.
            visible_current = (
                min(last_progress_current, reported_total)
                if reported_total
                else last_progress_current
            )
            updates = {
                "stage": stage,
                "progress_total": reported_total,
                "progress_current": visible_current,
                "message": _services().clean_text(message, 300),
            }
            # Persist while holding the same lock used to calculate the
            # monotonic counters.  Chunk workers and heartbeat threads can
            # finish out of order; releasing the lock before this write would
            # let an older snapshot reach SQLite after a newer one and make a
            # refreshed UI appear to move backwards.
            _services().update_ai_job(job_id, **updates)

    try:
        mode = (
            "reading_card"
            if job_type == "reading_card"
            else "deep_read" if job_type == "deep_read" else None
        )
        if is_abstract_brief:
            result = _services().generate_abstract_brief_analysis(
                job["paper_id"], progress_callback=progress
            )
        elif is_paragraph_explain:
            result = _services().analyze_paper_segment(
                job["paper_id"], options, progress_callback=progress
            )
        elif is_reading_segmentation:
            result = _services().organize_reading_blocks(
                job["paper_id"], options, progress_callback=progress
            )
        elif job_type == "deep_read" and synthesis_source:
            result = _services().run_deep_read_synthesis_retry(
                job["paper_id"], synthesis_source, progress_callback=progress
            )
        elif job_type == "prereading_guide":
            result = _services().run_prereading_guide_analysis(
                job["paper_id"], progress_callback=progress
            )
        else:
            try:
                result = _services().run_fulltext_ai_analysis(
                    job["paper_id"],
                    mode,
                    options.get("template", "graduate_deep_read"),
                    options.get("language", "zh-CN"),
                    progress_callback=progress,
                    strict_validation=True,
                    visual_enabled=options.get("visual_enabled", mode == "deep_read"),
                )
            except TypeError as exc:
                # Keep compatibility with local integrations that monkey-patch the
                # old four-argument function while still using the persistent job API.
                message = str(exc)
                if (
                    "strict_validation" not in message
                    and "progress_callback" not in message
                ):
                    raise
                try:
                    result = _services().run_fulltext_ai_analysis(
                        job["paper_id"],
                        mode,
                        options.get("template", "graduate_deep_read"),
                        options.get("language", "zh-CN"),
                        progress_callback=progress,
                    )
                except TypeError as legacy_exc:
                    if "progress_callback" not in str(legacy_exc):
                        raise
                    result = _services().run_fulltext_ai_analysis(
                        job["paper_id"],
                        mode,
                        options.get("template", "graduate_deep_read"),
                        options.get("language", "zh-CN"),
                    )
        if mode == "reading_card" and options.get("apply", True) is not False:
            with _services().DB_LOCK, _services().db_session() as db:
                row = db.execute(
                    "SELECT * FROM reading_cards WHERE paper_id=?", (job["paper_id"],)
                ).fetchone()
                existing = _services().reading_card_dict(row, job["paper_id"])
                merged = _services().merge_ai_reading_card(
                    existing,
                    result.get("draft", {}),
                    bool(options.get("merge_empty", True)),
                )
                card = _services().save_reading_card(db, job["paper_id"], merged, "ai")
                _services().record_audit(
                    db,
                    "reading_card.ai_applied",
                    "paper",
                    job["paper_id"],
                    {
                        "artifact_id": result.get("artifact_id", ""),
                        "merge_empty": bool(options.get("merge_empty", True)),
                        "job_id": job_id,
                    },
                )
                result["applied"] = card
        if _services()._ai_job_is_cancelled(job_id):
            raise _services().AIJobCancelled("任务已取消")
        partial = result.get("analysis_status") == "partial"
        success_message = (
            "已仅使用保存的 PDF 证据完成跨页综合"
            if synthesis_source
            else "摘要助读已生成并保存"
            if is_abstract_brief
            else "当前段落解释已保存"
            if is_paragraph_explain
            else "阅读块语义整理已保存"
            if is_reading_segmentation
            else "分析完成"
        )
        # Finish through the same monotonic progress path used by every
        # phase.  A deep-read may have reported a larger visual-page total;
        # writing ``chunk_count + 1`` directly here used to make the UI jump
        # backwards just as the job completed.
        final_total = max(int(result.get("chunk_count", 0)) + 1, last_progress_total, 1)
        retry_available = bool(result.get("synthesis_retry_available"))
        partial_message = (
            "模型暂不可用，已保存本地摘要摘录；修复模型配置或额度后可重新运行"
            if is_abstract_brief and result.get("fallback")
            else (
                "模型暂不可用，已保存 PDF 原文摘录；修复模型配置或额度后可完整重跑"
                if result.get("partial_kind") == "model_unavailable"
                else (
                    "正文引文未通过核验，请查看保留结果并完整重跑精读"
                    if result.get("partial_kind") == "evidence_gap"
                    else (
                        "已保存可核验分块发现，可仅重试跨页综合"
                        if retry_available
                        else "部分结果已保存，请查看证据缺口后完整重跑"
                    )
                )
            )
        )
        progress(
            "partial" if partial else "completed",
            final_total,
            final_total,
            partial_message if partial else success_message,
        )
        final_options = {**options, "synthesis_retry_available": retry_available}
        partial_error = (
            (
                _services().clean_text(result.get("fallback_reason", ""), 1000)
                if is_abstract_brief and result.get("fallback")
                else _services().clean_text(
                    result.get("analysis", {}).get("partial_reason", ""), 1000
                )
            )
            if partial
            else ""
        )
        _services().update_ai_job(
            job_id,
            status="partial" if partial else "success",
            stage="partial" if partial else "completed",
            message=partial_message if partial else success_message,
            error=partial_error,
            artifact_id=result.get("artifact_id", ""),
            options_json=_services().json.dumps(final_options, ensure_ascii=False),
            completed_at=_services().now_iso(),
            duration_ms=round(
                (_services().time.perf_counter() - job_started) * 1000, 2
            ),
        )
    except _services().AIJobCancelled as exc:
        _services().update_ai_job(
            job_id,
            status="cancelled",
            stage="cancelled",
            message=str(exc),
            error="",
            completed_at=_services().now_iso(),
            duration_ms=round(
                (_services().time.perf_counter() - job_started) * 1000, 2
            ),
        )
    except Exception as exc:
        api_key = _services().model_api_key()
        detail = _services().model_error_detail(exc, api_key)
        shutdown_race = _services()._is_interpreter_executor_shutdown(exc)
        _services().update_ai_job(
            job_id,
            status="interrupted" if shutdown_race else "failed",
            stage="interrupted" if shutdown_race else "failed",
            message=(
                _services().RUNTIME_SHUTDOWN_MESSAGE if shutdown_race else "分析失败"
            ),
            error=(
                "服务重启时任务未完成"
                if shutdown_race
                else _services().clean_text(detail, 1000)
            ),
            completed_at=_services().now_iso(),
            duration_ms=round(
                (_services().time.perf_counter() - job_started) * 1000, 2
            ),
        )
        with _services().DB_LOCK, _services().db_session() as db:
            event = (
                "ai.fulltext_analysis.interrupted"
                if shutdown_race
                else "ai.fulltext_analysis.failed"
            )
            _services().record_audit(
                db,
                event,
                "paper",
                job.get("paper_id", ""),
                {"job_id": job_id, "error": _services().clean_text(detail, 500)},
            )


def _mark_ai_job_interrupted_for_shutdown(
    job_id: str, message: str | None = None
) -> None:
    """Leave a durable resume marker when a worker cannot be submitted.

    ``ThreadPoolExecutor.submit`` raises when the interpreter or the service
    is already shutting down.  The old code left the row in ``queued`` in
    that case, which made the UI show a task that could never run.  Startup
    reconciliation only resumes explicit ``interrupted`` rows, so make that
    transition before surfacing the error to the caller.
    """
    if message is None:
        message = _services().RUNTIME_SHUTDOWN_MESSAGE
    try:
        _services().update_ai_job(
            job_id,
            status="interrupted",
            stage="interrupted",
            message=message,
            error="服务重启时任务未完成",
            completed_at=_services().now_iso(),
        )
    except Exception:
        # The original submit error is more useful than a secondary SQLite
        # failure during process teardown.
        pass


def submit_ai_job(paper_id: str, job_type: str, data: dict) -> dict:
    if job_type not in {
        "abstract_brief",
        "reading_card",
        "deep_read",
        "prereading_guide",
        "paragraph_explain",
        "reading_segmentation",
    }:
        raise ValueError("未知的 AI 任务类型")
    synthesis_only_artifact_id = (
        _services().clean_text(data.get("synthesis_only_artifact_id", ""), 100)
        if job_type == "deep_read"
        else ""
    )
    with _services().DB_LOCK, _services().db_session() as db:
        if not db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
            raise LookupError("论文不存在")
        if (
            job_type not in {"abstract_brief", "paragraph_explain"}
            and not synthesis_only_artifact_id
            and not _services().paper_has_fulltext_source(paper_id)
        ):
            raise ValueError(
                "这篇论文还没有可用的本地 PDF 或已提取全文；请先添加或下载 PDF，再开始全文分析"
            )
        active_rows = db.execute(
            "SELECT * FROM ai_jobs WHERE paper_id=? AND job_type=? AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 20",
            (paper_id, job_type),
        ).fetchall()
        active = active_rows[0] if active_rows else None
        if job_type == "paragraph_explain":
            requested_key = _services().clean_text(data.get("segment_key", ""), 80)
            active = None
            for candidate in active_rows:
                try:
                    candidate_options = _services().json.loads(candidate["options_json"] or "{}")
                except (TypeError, ValueError):
                    candidate_options = {}
                if candidate_options.get("segment_key") == requested_key:
                    active = candidate
                    break
        if active:
            return {"job": _services().ai_job_snapshot(active), "deduplicated": True}
        job_id = str(_services().uuid.uuid4())
        created = _services().now_iso()
        default_template = {
            "abstract_brief": "abstract-brief-v2-structured",
            "reading_card": "fulltext_reading_card",
            "deep_read": "graduate_deep_read",
            "prereading_guide": "paper_prereading_guide",
            "paragraph_explain": "paragraph_explain_v1",
            "reading_segmentation": "semantic_reading_blocks_v1",
        }[job_type]
        # Quick reading cards are text-first and never need page images. Deep
        # read keeps visual recognition enabled by default, but callers can
        # explicitly set visual_enabled=false to run a low-cost text-only
        # pass (useful for testing, OCR-heavy papers, or text-only relays).
        default_visual_enabled = job_type == "deep_read"
        requested_visuals = data.get("visual_enabled", default_visual_enabled)
        if job_type in {"reading_card", "deep_read"} and not isinstance(
            requested_visuals, bool
        ):
            raise ValueError("visual_enabled 必须是布尔值")
        options = {
            "template": _services().clean_text(
                data.get("template", default_template), 120
            ),
            "language": _services().clean_text(data.get("language", "zh-CN"), 30),
            "apply": data.get("apply", True) is not False,
            "merge_empty": data.get("merge_empty", True) is not False,
            "visual_enabled": (
                requested_visuals
                if job_type in {"reading_card", "deep_read"}
                else False
            ),
        }
        if job_type == "reading_segmentation":
            if data.get("consent") is not True:
                raise ValueError("请先确认允许将论文短文本片段发送给模型判断分块")
            options["consent"] = True
            options["force"] = data.get("force") is True
        if job_type == "paragraph_explain":
            for key in ("segment_key", "text", "previous_context", "next_context"):
                options[key] = _services().clean_text(
                    data.get(key, ""), 9000 if key == "text" else 1000
                )
            if not options["segment_key"] or not options["text"]:
                raise ValueError("缺少段落内容或段落标识")
        if synthesis_only_artifact_id:
            source = db.execute(
                "SELECT answer FROM ai_artifacts WHERE id=? AND paper_id=? AND artifact_type='deep_read'",
                (synthesis_only_artifact_id, paper_id),
            ).fetchone()
            if not source:
                raise LookupError("找不到同一篇论文的已保存精读证据")
            try:
                source_answer = _services().json.loads(source["answer"] or "{}")
            except (TypeError, ValueError, _services().json.JSONDecodeError) as exc:
                raise ValueError("已保存的精读证据格式损坏，无法仅重试综合") from exc
            source_meta = (
                source_answer.get("meta")
                if isinstance(source_answer.get("meta"), dict)
                else {}
            )
            if source_meta.get("analysis_status") != "partial":
                raise ValueError("仅重试综合只接受部分完成的精读结果")
            if source_meta.get("synthesis_retry_available") is False:
                raise ValueError("这条部分结果没有可复用的页码证据，请完整重新运行精读")
            options["synthesis_only_artifact_id"] = synthesis_only_artifact_id
        # A completed artifact is reusable when the local source, model and
        # analysis template are unchanged. This is the important long-term
        # speed path: reopening a paper or refreshing the browser must not
        # trigger another expensive multimodal run.
        if job_type not in {"abstract_brief", "paragraph_explain", "reading_segmentation"} and data.get("reuse", True) is not False:
            source_fingerprint = _services().fulltext_source_fingerprint(paper_id)
            if source_fingerprint:
                artifact_type = {
                    "reading_card": "reading_card_draft",
                    "deep_read": "deep_read",
                    "prereading_guide": "prereading_guide",
                }[job_type]
                settings = _services().resolve_model_route(
                    _services().model_settings(), "analysis"
                )
                expected_identity = {
                    "model": settings.get("model", ""),
                    "protocol": settings.get("protocol", ""),
                    "endpoint": _services().model_endpoint(settings),
                }
                if job_type == "deep_read" and bool(
                    options.get("visual_enabled", True)
                ):
                    expected_identity["vision_model"] = (
                        _services()
                        .resolve_model_route(settings, "vision")
                        .get("model", "")
                    )
                artifact_rows = db.execute(
                    "SELECT * FROM ai_artifacts WHERE paper_id=? AND artifact_type=? ORDER BY created_at DESC LIMIT 8",
                    (paper_id, artifact_type),
                ).fetchall()
                for artifact in artifact_rows:
                    try:
                        saved = _services().json.loads(artifact["answer"] or "{}")
                        meta = (
                            saved.get("meta")
                            if isinstance(saved.get("meta"), dict)
                            else {}
                        )
                    except (TypeError, ValueError, _services().json.JSONDecodeError):
                        continue
                    if meta.get("analysis_status") == "partial":
                        continue
                    pipeline_version = meta.get("pipeline_version")
                    compatible_pipelines = {
                        "reading_card": {
                            _services().READING_CARD_PIPELINE_VERSION,
                            _services().DEEP_READ_PIPELINE_VERSION,
                        },
                        "deep_read": {_services().DEEP_READ_PIPELINE_VERSION},
                        "prereading_guide": {
                            _services().PREREADING_GUIDE_PIPELINE_VERSION
                        },
                    }[job_type]
                    if (
                        meta.get("source_fingerprint") != source_fingerprint
                        or meta.get("template") != options["template"]
                        or meta.get("language") != options["language"]
                        or meta.get("model_identity") != expected_identity
                        or (
                            job_type in {"reading_card", "deep_read"}
                            and bool(
                                meta.get("visual_enabled", job_type == "deep_read")
                            )
                            != bool(
                                options.get("visual_enabled", job_type == "deep_read")
                            )
                        )
                        or pipeline_version not in compatible_pipelines
                    ):
                        continue
                    # Do not resurrect legacy artifacts whose JSON was valid
                    # but whose card fields are only placeholders such as
                    # ``...`` or ``原文未说明。``. They must go through a
                    # fresh bounded repair instead of appearing successful.
                    if job_type == "reading_card":
                        saved_analysis = (
                            saved.get("analysis")
                            if isinstance(saved.get("analysis"), dict)
                            else {}
                        )
                        try:
                            _services().validate_fulltext_analysis(
                                "reading_card", saved_analysis
                            )
                        except ValueError:
                            continue
                    if job_type == "reading_card" and options["apply"]:
                        analysis = (
                            saved.get("analysis")
                            if isinstance(saved.get("analysis"), dict)
                            else {}
                        )
                        draft = (
                            analysis.get("reading_card")
                            if isinstance(analysis.get("reading_card"), dict)
                            else {}
                        )
                        row = db.execute(
                            "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
                        ).fetchone()
                        existing = _services().reading_card_dict(row, paper_id)
                        merged = _services().merge_ai_reading_card(
                            existing, draft, options["merge_empty"]
                        )
                        _services().save_reading_card(db, paper_id, merged, "ai")
                        _services().record_audit(
                            db,
                            "reading_card.ai_reused",
                            "paper",
                            paper_id,
                            {"artifact_id": artifact["id"], "job_id": job_id},
                        )
                    progress_total = max(int(meta.get("chunk_count") or 0) + 1, 1)
                    db.execute(
                        "INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,progress_current,progress_total,message,artifact_id,options_json,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            job_id,
                            paper_id,
                            job_type,
                            "success",
                            "reused",
                            progress_total,
                            progress_total,
                            "已复用相同全文输入的历史分析",
                            artifact["id"],
                            _services().json.dumps(options, ensure_ascii=False),
                            created,
                            created,
                            created,
                        ),
                    )
                    row = db.execute(
                        "SELECT * FROM ai_jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    return {
                        "job": _services().ai_job_snapshot(row),
                        "deduplicated": True,
                        "reused": True,
                        "artifact_id": artifact["id"],
                    }
        db.execute(
            "INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,message,options_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                paper_id,
                job_type,
                "queued",
                "queued",
                "已加入任务队列",
                _services().json.dumps(options, ensure_ascii=False),
                created,
                created,
            ),
        )
        row = db.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    # Do not create a new worker after the server has begun a controlled
    # shutdown.  The durable row is kept as ``interrupted`` so the next
    # process can resume it instead of silently losing the request.
    if _services().STOP_EVENT.is_set():
        _services()._mark_ai_job_interrupted_for_shutdown(job_id)
        raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
    scope_key = _services()._current_user_scope_key()
    if scope_key:
        with _services().USER_AI_JOB_EXECUTORS_LOCK:
            executor = _services().USER_AI_JOB_EXECUTORS.get(scope_key)
            if executor is None:
                executor = _services().ThreadPoolExecutor(
                    max_workers=_services().AI_JOB_CONCURRENCY,
                    thread_name_prefix="ai-job-user",
                )
                _services().USER_AI_JOB_EXECUTORS[scope_key] = executor
            try:
                executor.submit(_services().run_ai_job, job_id)
            except RuntimeError as exc:
                _services().USER_AI_JOB_EXECUTORS.pop(scope_key, None)
                _services()._mark_ai_job_interrupted_for_shutdown(job_id)
                raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE) from exc
        return {"job": _services().ai_job_snapshot(row), "deduplicated": False}
    with _services().AI_JOB_EXECUTOR_LOCK:
        if _services().STOP_EVENT.is_set():
            _services()._mark_ai_job_interrupted_for_shutdown(job_id)
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
        if _services().AI_JOB_EXECUTOR is None:
            _services().AI_JOB_EXECUTOR = _services().ThreadPoolExecutor(
                max_workers=_services().AI_JOB_CONCURRENCY, thread_name_prefix="ai-job"
            )
        executor = _services().AI_JOB_EXECUTOR
        try:
            executor.submit(_services().run_ai_job, job_id)
        except RuntimeError as exc:
            # This covers a race with executor.shutdown() as well as Python's
            # interpreter-shutdown guard.  Reset the handle so a later
            # request after a clean restart can create a fresh executor.
            if _services().AI_JOB_EXECUTOR is executor:
                _services().AI_JOB_EXECUTOR = None
            _services()._mark_ai_job_interrupted_for_shutdown(job_id)
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE) from exc
    return {"job": _services().ai_job_snapshot(row), "deduplicated": False}


def retry_ai_job(job_id: str) -> dict:
    """Create a fresh persistent attempt for a terminal AI task.

    Retrying is explicit and idempotent: an active attempt is returned instead
    of creating a second model request, while a successful artifact is never
    silently recomputed.
    """
    job = _services().get_ai_job(job_id)
    if not job:
        raise LookupError("AI 任务不存在")
    if job.get("status") == "success":
        return {"job": job, "deduplicated": True, "retried_from": job_id}
    if job.get("status") in {"queued", "running"}:
        return {"job": job, "deduplicated": True, "retried_from": job_id}
    options = job.get("options") if isinstance(job.get("options"), dict) else {}
    synthesis_source = _services().clean_text(
        options.get("synthesis_only_artifact_id", ""), 100
    )
    if (
        job.get("status") == "partial"
        and job.get("job_type") == "deep_read"
        and job.get("artifact_id")
    ):
        synthesis_source = _services().clean_text(job["artifact_id"], 100)
    if (
        job.get("status") in {"partial", "failed", "interrupted"}
        and job.get("job_type") == "deep_read"
        and synthesis_source
    ):
        # The submit path performs the authoritative same-paper/partial
        # artifact validation.  Keep an explicitly supplied source intact
        # when a caller is backed by a remote or test double; a real complete
        # artifact is still rejected by submit_ai_job before any model call.
        verified_source = _services()._partial_deep_read_artifact_id(
            job["paper_id"], synthesis_source
        )
        if verified_source:
            synthesis_source = verified_source
        elif job.get("status") == "partial" and job.get("artifact_id"):
            synthesis_source = ""
    if (
        job.get("status") in {"partial", "failed", "interrupted"}
        and job.get("job_type") == "deep_read"
        and synthesis_source
    ):
        data = {
            **options,
            "synthesis_only_artifact_id": synthesis_source,
            "reuse": False,
        }
        result = _services().submit_ai_job(job["paper_id"], "deep_read", data)
        result["retried_from"] = job_id
        return result
    result = _services().submit_ai_job(job["paper_id"], job["job_type"], options)
    result["retried_from"] = job_id
    return result


def resume_interrupted_ai_jobs(limit: int = 20) -> list[dict]:
    """Requeue AI work interrupted by a process restart.

    The old row remains an immutable audit record. A new durable job carries
    on the work, and a partial deep-read artifact is used for synthesis-only
    recovery so already verified PDF evidence is never discarded.
    """
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT id FROM ai_jobs WHERE status='interrupted' AND cancel_requested=0 "
            "AND error IN ('服务重启时任务未完成','服务重启时任务尚未开始') "
            "ORDER BY created_at LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    resumed = []
    for row in rows:
        old_id = row["id"]
        job = _services().get_ai_job(old_id)
        if not job:
            continue
        options = job.get("options") if isinstance(job.get("options"), dict) else {}
        data = dict(options)
        source_id = ""
        if job.get("job_type") == "deep_read":
            source_id = _services()._partial_deep_read_artifact_id(
                job.get("paper_id", ""), job.get("artifact_id", "")
            )
            if source_id:
                data.update({"synthesis_only_artifact_id": source_id, "reuse": False})
        try:
            result = _services().submit_ai_job(job["paper_id"], job["job_type"], data)
            new_job = result.get("job") if isinstance(result, dict) else {}
            new_id = (
                _services().clean_text(new_job.get("id", ""), 100)
                if isinstance(new_job, dict)
                else ""
            )
            with _services().DB_LOCK, _services().db_session() as db:
                db.execute(
                    "UPDATE ai_jobs SET message=?,error=?,updated_at=? WHERE id=?",
                    (
                        f"已自动恢复到任务 {new_id}" if new_id else "已自动恢复任务",
                        "",
                        _services().now_iso(),
                        old_id,
                    ),
                )
                _services().record_audit(
                    db,
                    "ai.job.resumed",
                    "paper",
                    job.get("paper_id", ""),
                    {
                        "interrupted_job_id": old_id,
                        "resumed_job_id": new_id,
                        "job_type": job.get("job_type", ""),
                        "synthesis_only": bool(source_id),
                    },
                )
            resumed.append(
                {
                    "interrupted_job_id": old_id,
                    "resumed_job_id": new_id,
                    "deduplicated": (
                        bool(result.get("deduplicated"))
                        if isinstance(result, dict)
                        else False
                    ),
                }
            )
        except Exception as exc:
            detail = _services().clean_text(
                _services().model_error_detail(exc, _services().model_api_key()), 800
            )
            with _services().DB_LOCK, _services().db_session() as db:
                db.execute(
                    "UPDATE ai_jobs SET message=?,error=?,updated_at=? WHERE id=?",
                    ("自动恢复失败，可手动重试", detail, _services().now_iso(), old_id),
                )
                _services().record_audit(
                    db,
                    "ai.job.resume_failed",
                    "paper",
                    job.get("paper_id", ""),
                    {"interrupted_job_id": old_id, "error": detail},
                )
    return resumed


def resume_interrupted_sync_jobs(limit: int = 20) -> list[dict]:
    """Resume only searches interrupted by this process startup.

    Search runs keep their query, sources, options and planner snapshot in
    SQLite. Reusing the original run ID lets the worker continue publishing
    into the same durable history item after a browser refresh or service
    restart. Older interrupted rows are not replayed automatically: they are
    historical failures and require an explicit retry from the user.
    """
    with _services().DB_LOCK:
        run_ids = list(dict.fromkeys(_services().STARTUP_INTERRUPTED_SYNC_IDS))[
            : max(1, min(int(limit), 100))
        ]
        # Claim the in-memory startup list before launching threads. If a
        # caller invokes this helper twice during startup, the second call
        # cannot create a duplicate worker for the same run.
        _services().STARTUP_INTERRUPTED_SYNC_IDS = []
    resumed = []
    for job_id in run_ids:
        with _services().DB_LOCK, _services().db_session() as db:
            row = db.execute(
                """SELECT s.id,s.query_id,s.run_id,s.status,s.last_error,
                          r.query_name,r.sources_json,r.is_scheduled,r.status AS run_status,
                          rs.query AS snapshot_query,rs.plan_json
                   FROM sync_jobs s
                   LEFT JOIN runs r ON r.id=s.run_id
                   LEFT JOIN run_snapshots rs ON rs.run_id=s.run_id
                   WHERE s.id=?""",
                (job_id,),
            ).fetchone()
        if (
            not row
            or row["status"] != "interrupted"
            or row["last_error"]
            not in {"服务重启时任务未完成", "服务重启时任务尚未开始"}
        ):
            continue
        plan = _services()._json_object(row["plan_json"] if row["plan_json"] else "{}")
        options = _services().normalize_search_options(
            plan.get("search_options") or plan.get("filters") or {}
        )
        query = _services().clean_text(row["snapshot_query"] or row["query_name"], 1000)
        if not query:
            continue
        try:
            sources = _services().json.loads(row["sources_json"] or "[]")
        except (TypeError, ValueError, _services().json.JSONDecodeError):
            sources = []
        if not isinstance(sources, list):
            sources = []
        sources = [
            source
            for source in sources
            if source in _services().SOURCE_NAMES
            and source in _services().SOURCE_SEARCH
        ]
        try:
            result_limit = min(100, max(1, int(plan.get("result_limit") or 25)))
        except (TypeError, ValueError):
            result_limit = 25
        plan_override = (
            plan
            if isinstance(plan.get("query_variants"), list)
            and plan.get("query_variants")
            else None
        )
        if plan_override:
            plan_override = dict(plan_override)
            plan_override["search_options"] = options
            plan_override["original_query"] = query
            plan_override["query"] = query
        # Move the existing run back to queued before launching its worker.
        # This state is durable and makes a second process observer show that
        # recovery has been claimed rather than leaving a false interruption.
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "UPDATE sync_jobs SET status='queued',stage='resume_queued',last_error='',attempts=attempts+1,updated_at=? WHERE id=? AND status='interrupted'",
                (_services().now_iso(), job_id),
            )
            db.execute(
                "UPDATE runs SET status='queued',completed_at='',error='' WHERE id=? AND status='interrupted'",
                (row["run_id"],),
            )
            _services().record_audit(
                db,
                "sync.resume.claimed",
                "run",
                row["run_id"],
                {"source": "startup", "query": query},
            )
        try:
            result = _services().launch_query_job(
                query,
                sources,
                result_limit,
                query_id=_services().clean_text(row["query_id"], 120),
                scheduled=bool(row["is_scheduled"]),
                name=_services().clean_text(row["query_name"], 500),
                options=options,
                request_id=_services().clean_text(row["run_id"], 120),
                plan_override=plan_override,
            )
            resumed.append(
                {
                    "interrupted_job_id": job_id,
                    "resumed_run_id": _services().clean_text(row["run_id"], 120),
                    "status": (
                        result.get("status", "queued")
                        if isinstance(result, dict)
                        else "queued"
                    ),
                }
            )
        except Exception as exc:
            detail = _services().clean_text(str(exc), 800)
            with _services().DB_LOCK, _services().db_session() as db:
                db.execute(
                    "UPDATE sync_jobs SET status='failed',stage='failed',last_error=?,updated_at=? WHERE id=?",
                    (detail, _services().now_iso(), job_id),
                )
                db.execute(
                    "UPDATE runs SET status='failed',completed_at=?,error=? WHERE id=?",
                    (_services().now_iso(), detail, row["run_id"]),
                )
                _services().record_audit(
                    db, "sync.resume.failed", "run", row["run_id"], {"error": detail}
                )
    return resumed


__all__ = [
    "_current_user_scope_key",
    "_register_background_thread",
    "_unregister_background_thread",
    "shutdown_background_workers",
    "scheduled_workspace_tick",
    "scheduler_loop",
    "AIJobCancelled",
    "ai_job_snapshot",
    "update_ai_job",
    "get_ai_job",
    "_partial_deep_read_artifact_id",
    "run_deep_read_synthesis_retry",
    "_ai_job_is_cancelled",
    "_is_interpreter_executor_shutdown",
    "run_ai_job",
    "_mark_ai_job_interrupted_for_shutdown",
    "submit_ai_job",
    "retry_ai_job",
    "resume_interrupted_ai_jobs",
    "resume_interrupted_sync_jobs",
]
