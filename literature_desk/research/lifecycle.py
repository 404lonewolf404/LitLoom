"""Run scheduling, resume, approval, and result application."""

from __future__ import annotations

import sqlite3

from ..services import app_services as _services


def _mark_agent_run_interrupted_for_shutdown(
    run_id: str, message: str | None = None
) -> None:
    """Persist an Agent run when its worker cannot be scheduled during stop."""
    if message is None:
        message = _services().RUNTIME_SHUTDOWN_MESSAGE
    try:
        with _services().DB_LOCK, _services().db_session() as db:
            now = _services().now_iso()
            db.execute(
                "UPDATE agent_runs SET status='interrupted',error='服务重启时任务未完成',updated_at=? WHERE id=? AND status IN ('queued','running')",
                (now, run_id),
            )
            db.execute(
                "UPDATE agent_steps SET status='queued',error='服务重启时任务未完成',updated_at=? WHERE run_id=? AND status='running'",
                (now, run_id),
            )
    except Exception:
        pass


def _enqueue_agent_run(run_id: str) -> None:
    """Submit an Agent workflow without leaving a queued zombie."""
    if _services().STOP_EVENT.is_set():
        _services()._mark_agent_run_interrupted_for_shutdown(run_id)
        raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
    actor = _services().AUTH_ACTOR.get() or {}
    scope_key = (
        f"{actor.get('workspace_id','')}:{actor.get('user_id','')}"
        if actor.get("user_id")
        else ""
    )
    if scope_key:
        with _services().USER_AGENT_RUN_EXECUTORS_LOCK:
            executor = _services().USER_AGENT_RUN_EXECUTORS.get(scope_key)
            if executor is None:
                executor = _services().ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="research-agent-user"
                )
                _services().USER_AGENT_RUN_EXECUTORS[scope_key] = executor
            try:
                executor.submit(_services().run_agent_workflow, run_id)
            except RuntimeError as exc:
                _services().USER_AGENT_RUN_EXECUTORS.pop(scope_key, None)
                _services()._mark_agent_run_interrupted_for_shutdown(run_id)
                raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE) from exc
        return
    with _services().AGENT_RUN_EXECUTOR_LOCK:
        if _services().STOP_EVENT.is_set():
            _services()._mark_agent_run_interrupted_for_shutdown(run_id)
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
        if _services().AGENT_RUN_EXECUTOR is None:
            _services().AGENT_RUN_EXECUTOR = _services().ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="research-agent"
            )
        executor = _services().AGENT_RUN_EXECUTOR
        try:
            executor.submit(_services().run_agent_workflow, run_id)
        except RuntimeError as exc:
            if _services().AGENT_RUN_EXECUTOR is executor:
                _services().AGENT_RUN_EXECUTOR = None
            _services()._mark_agent_run_interrupted_for_shutdown(run_id)
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE) from exc


def resume_interrupted_agent_runs(limit: int = 20) -> list[dict]:
    """Resume only Agent runs interrupted by the current process startup."""
    with _services().DB_LOCK:
        run_ids = list(dict.fromkeys(_services().STARTUP_INTERRUPTED_AGENT_IDS))[
            : max(1, min(int(limit), 100))
        ]
        _services().STARTUP_INTERRUPTED_AGENT_IDS = []
    resumed = []
    for run_id in run_ids:
        with _services().DB_LOCK, _services().db_session() as db:
            row = db.execute(
                "SELECT id,status,error,workflow,budget_json FROM agent_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if (
                not row
                or row["status"] != "interrupted"
                or row["error"] != "服务重启时任务未完成"
            ):
                continue
            if row["workflow"] == "autonomous":
                budget = _services()._json_value(row["budget_json"], {})
                budget["consent_external"] = False
                db.execute(
                    "UPDATE agent_runs SET error=?,budget_json=?,updated_at=? WHERE id=?",
                    (
                        "服务重启后暂停自主运行；检查工具记录并确认后再继续",
                        _services().json.dumps(budget, ensure_ascii=False),
                        _services().now_iso(),
                        run_id,
                    ),
                )
                continue
            now = _services().now_iso()
            db.execute(
                "UPDATE agent_runs SET status='queued',error='',completed_at='',updated_at=? WHERE id=?",
                (now, run_id),
            )
            db.execute(
                "UPDATE agent_steps SET status='queued',error='',completed_at='',updated_at=? WHERE run_id=? AND status IN ('running','queued')",
                (now, run_id),
            )
        try:
            _services()._enqueue_agent_run(run_id)
            resumed.append({"interrupted_run_id": run_id, "resumed_run_id": run_id})
        except Exception as exc:
            _services()._mark_agent_run_interrupted_for_shutdown(run_id)
            with _services().DB_LOCK, _services().db_session() as db:
                _services().record_audit(
                    db,
                    "agent.run_resume_failed",
                    "agent_run",
                    run_id,
                    {"error": _services().clean_text(str(exc), 500)},
                )
    return resumed


def approve_agent_run(run_id: str, consent_external: bool = False) -> dict:
    run = _services().agent_run_snapshot(run_id)
    if not run:
        raise LookupError("Agent 运行不存在")
    if run["status"] not in {
        "awaiting_confirmation",
        "paused",
        "queued",
        "interrupted",
        "failed",
    }:
        return run
    with _services().DB_LOCK, _services().db_session() as db:
        budget = dict(run.get("budget") or {})
        for step in run.get("steps", []):
            if step["status"] == "failed":
                if int(step.get("attempts") or 0) >= int(step.get("max_attempts") or 2):
                    raise ValueError(
                        "此步骤已达到自动重试上限，请检查失败原因后新建一次运行"
                    )
                db.execute(
                    "UPDATE agent_steps SET status='queued',error='',completed_at='',updated_at=? WHERE id=?",
                    (_services().now_iso(), step["id"]),
                )
            elif step["status"] == "awaiting_confirmation":
                db.execute(
                    "UPDATE agent_steps SET status='queued',error='',message='',updated_at=? WHERE id=?",
                    (_services().now_iso(), step["id"]),
                )
        budget["consent_external"] = bool(consent_external)
        db.execute(
            "UPDATE agent_runs SET status='queued',error='',budget_json=?,completed_at='',updated_at=? WHERE id=?",
            (
                _services().json.dumps(budget, ensure_ascii=False),
                _services().now_iso(),
                run_id,
            ),
        )
    _services()._enqueue_agent_run(run_id)
    return _services().agent_run_snapshot(run_id)


def pause_agent_run(run_id: str) -> dict:
    run = _services().agent_run_snapshot(run_id, include_steps=False)
    if not run:
        raise LookupError("Agent 运行不存在")
    if run["status"] in {"queued", "running"}:
        _services()._agent_update_run(run_id, status="paused", error="")
    return _services().agent_run_snapshot(run_id)


def import_agent_candidates(
    run_id: str, candidate_ids: list[str], confirm: bool
) -> dict:
    if confirm is not True:
        raise PermissionError("将候选写入文献库需要显式确认")
    selected = list(
        dict.fromkeys(
            _services().clean_text(value, 160)
            for value in candidate_ids
            if _services().clean_text(value, 160)
        )
    )
    if not selected or len(selected) > 100:
        raise ValueError("请选择 1 到 100 篇候选论文")
    run = _services().agent_run_snapshot(run_id, include_steps=False)
    if not run:
        raise LookupError("Agent 运行不存在")
    if run.get("workflow") == "autonomous":
        detail = _services().agent_run_snapshot(run_id)
        candidate_sources: dict[str, str] = {}
        candidate_records: dict[str, dict] = {}
        for step in (detail or {}).get("steps", []):
            if step.get("status") != "success" or step.get("tool_name") not in {
                "search_sources",
                "expand_search",
                "expand_citations",
            }:
                continue
            output = step.get("output") or {}
            source_run_id = _services().clean_text(output.get("run_id", ""), 80)
            papers = output.get("papers") or output.get("candidates") or []
            for paper in papers if isinstance(papers, list) else []:
                if not isinstance(paper, dict) or not str(
                    paper.get("id") or ""
                ).startswith("candidate:"):
                    continue
                candidate_id = str(paper["id"])
                candidate_records[candidate_id] = paper
                if source_run_id:
                    candidate_sources[candidate_id] = source_run_id
        if any(candidate_id not in candidate_records for candidate_id in selected):
            raise ValueError("所选论文不属于本次自主 Agent 的检索候选")
        imported_totals = {
            "imported": 0,
            "duplicates": 0,
            "paper_ids": [],
            "candidate_map": {},
        }
        grouped: dict[str, list[str]] = {}
        for candidate_id in selected:
            source_run_id = candidate_sources.get(candidate_id)
            if source_run_id:
                grouped.setdefault(source_run_id, []).append(candidate_id)
        for source_run_id, values in grouped.items():
            imported = _services().import_discovery_candidates(source_run_id, values)
            imported_totals["imported"] += int(imported.get("imported") or 0)
            imported_totals["duplicates"] += int(imported.get("duplicates") or 0)
            imported_totals["paper_ids"].extend(imported.get("paper_ids") or [])
            imported_totals["candidate_map"].update(imported.get("candidate_map") or {})
        for candidate_id in selected:
            if candidate_id in candidate_sources:
                continue
            record = candidate_records[candidate_id]
            paper_id, created = _services().upsert_paper(
                record,
                source=record.get("source", "agent") or "agent",
                external_id=record.get("external_id", "") or record.get("doi", ""),
                raw=record,
            )
            imported_totals["paper_ids"].append(paper_id)
            imported_totals["candidate_map"][candidate_id] = paper_id
            imported_totals["imported" if created else "duplicates"] += 1
        return {"run_id": run_id, **imported_totals}
    with _services().DB_LOCK, _services().db_session() as db:
        search_step = db.execute(
            "SELECT output_json FROM agent_steps WHERE run_id=? AND step_key='search_candidates'",
            (run_id,),
        ).fetchone()
        step_rows = db.execute(
            "SELECT step_key,output_json FROM agent_steps WHERE run_id=? AND step_key IN ('search_candidates','expand_search','expand_citations','triage_candidates')",
            (run_id,),
        ).fetchall()
    search_output = _services()._json_value(search_step[0] if search_step else "", {})
    search_run_id = _services().clean_text(search_output.get("run_id", ""), 80)
    if not search_run_id:
        raise ValueError("此 Agent 运行尚无已完成的候选检索")
    snapshot_records = {}
    for row in step_rows:
        output = _services()._json_value(row["output_json"], {})
        values = (
            output.get("papers")
            if row["step_key"] != "triage_candidates"
            else output.get("candidates")
        )
        for item in values if isinstance(values, list) else []:
            if isinstance(item, dict) and item.get("id"):
                snapshot_records[item["id"]] = item
    with _services().DB_LOCK, _services().db_session() as db:
        placeholders = ",".join("?" for _ in selected)
        durable_ids = {
            row["id"]
            for row in db.execute(
                f"SELECT id FROM discovery_candidates WHERE run_id=? AND id IN ({placeholders})",
                (search_run_id, *selected),
            ).fetchall()
        }
    result = (
        _services().import_discovery_candidates(search_run_id, sorted(durable_ids))
        if durable_ids
        else {
            "run_id": search_run_id,
            "imported": 0,
            "duplicates": 0,
            "paper_ids": [],
            "candidate_map": {},
        }
    )
    for candidate_id in selected:
        if candidate_id in durable_ids:
            continue
        record = snapshot_records.get(candidate_id)
        if not record:
            continue
        paper_id, created = _services().upsert_paper(
            record,
            source=record.get("source", "manual") or "manual",
            external_id=record.get("external_id", "") or record.get("doi", ""),
            raw=record,
        )
        result["paper_ids"].append(paper_id)
        result["candidate_map"][candidate_id] = paper_id
        result["imported"] += 1
        result["duplicates"] += int(not created)
    result["selected"] = len(selected)
    with _services().DB_LOCK, _services().db_session() as db:
        _services().record_audit(
            db,
            "agent.candidates_imported",
            "agent_run",
            run_id,
            {"selected": len(selected), "imported": result["imported"]},
        )
    return result | {"agent_run_id": run_id, "confirmed": True}


def download_agent_fulltext(
    run_id: str, candidate_ids: list[str], confirm: bool
) -> dict:
    """Download only selected, already-imported candidates for an Agent run."""
    if confirm is not True:
        raise PermissionError("下载公开 PDF 需要显式确认")
    selected = list(
        dict.fromkeys(
            _services().clean_text(value, 200)
            for value in candidate_ids
            if _services().clean_text(value, 200)
        )
    )
    if not selected or len(selected) > 8:
        raise ValueError("请选择 1 到 8 篇论文下载 PDF")
    run = _services().agent_run_snapshot(run_id)
    if not run:
        raise LookupError("Agent 运行不存在")
    triage = next(
        (
            step.get("output") or {}
            for step in run.get("steps", [])
            if step.get("step_key") == "triage_candidates"
        ),
        {},
    )
    candidates = triage.get("candidates") if isinstance(triage, dict) else []
    by_id = {
        _services().clean_text(item.get("local_paper_id"), 200): item
        for item in candidates
        if isinstance(item, dict) and item.get("local_paper_id")
    }
    missing = [paper_id for paper_id in selected if paper_id not in by_id]
    if missing:
        raise ValueError("只能下载当前 Agent 运行已筛选且已导入文献库的论文")
    result = _services().call_agent_tool(
        "download_fulltext",
        {
            "run_id": run_id,
            "paper_ids": selected,
            "papers": [by_id[item] for item in selected],
            "confirm": True,
        },
    )
    with _services().DB_LOCK, _services().db_session() as db:
        _services().record_audit(
            db,
            "agent.fulltext_downloaded",
            "agent_run",
            run_id,
            {
                "selected": selected,
                "downloaded": result.get("downloaded_count", 0),
                "failed": result.get("failed_count", 0),
            },
        )
    return result | {"agent_run_id": run_id, "confirmed": True}


def apply_agent_reading_card(run_id: str, paper_id: str, confirm: bool) -> dict:
    """Apply a completed Agent draft without replacing human fields."""
    if confirm is not True:
        raise PermissionError("写入阅读卡需要显式确认")
    run = _services().agent_run_snapshot(run_id)
    if not run:
        raise LookupError("Agent 运行不存在")
    draft = next(
        (
            step.get("output") or {}
            for step in run.get("steps", [])
            if step.get("step_key") == "draft_reading_card"
        ),
        {},
    )
    card = draft.get("reading_card") if isinstance(draft, dict) else None
    if not isinstance(card, dict) or not card:
        raise ValueError("此运行没有已完成的阅读卡草稿")
    target_id = _services().clean_text(paper_id or draft.get("paper_id", ""), 200)
    result = _services().call_agent_tool(
        "save_reading_card",
        {
            "paper_id": target_id,
            "reading_card": card,
            "merge_empty": True,
            "run_id": run_id,
            "confirm": True,
        },
    )
    with _services().DB_LOCK, _services().db_session() as db:
        _services().record_audit(
            db,
            "agent.reading_card_applied",
            "agent_run",
            run_id,
            {"paper_id": target_id},
        )
    return result | {"agent_run_id": run_id, "confirmed": True}


def export_agent_report(run_id: str, content: str, confirm: bool) -> dict:
    """Export a selected Agent artifact as a markdown report after approval."""
    if confirm is not True:
        raise PermissionError("导出研究报告需要显式确认")
    run = _services().agent_run_snapshot(run_id)
    if not run:
        raise LookupError("Agent 运行不存在")
    report = _services().clean_text(content, 200000)
    if not report:
        artifact = (run.get("artifacts") or [None])[0]
        if artifact:
            report = _services().json.dumps(
                artifact.get("content") or {}, ensure_ascii=False, indent=2
            )
    result = _services().call_agent_tool(
        "export_report", {"content": report, "run_id": run_id, "confirm": True}
    )
    with _services().DB_LOCK, _services().db_session() as db:
        _services().record_audit(
            db,
            "agent.report_exported",
            "agent_run",
            run_id,
            {"content_sha256": result.get("content_sha256", "")},
        )
    return result | {"agent_run_id": run_id, "confirmed": True}


def cancel_agent_run(run_id: str) -> dict:
    run = _services().agent_run_snapshot(run_id, include_steps=False)
    if not run:
        raise LookupError("Agent 运行不存在")
    # Cancellation is a request against live work.  A late browser retry or
    # refresh must never rewrite a durable success/partial/failure result into
    # ``cancelled`` after the worker has already reached a terminal state.
    if run.get("status") in {"success", "partial", "failed", "cancelled"}:
        return run
    _services()._agent_update_run(
        run_id, status="cancelled", error="用户取消", completed_at=_services().now_iso()
    )
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "UPDATE agent_steps SET status='cancelled',updated_at=?,completed_at=? WHERE run_id=? AND status IN ('queued','running','awaiting_confirmation')",
            (_services().now_iso(), _services().now_iso(), run_id),
        )
    return _services().agent_run_snapshot(run_id)


__all__ = [
    "_mark_agent_run_interrupted_for_shutdown",
    "_enqueue_agent_run",
    "resume_interrupted_agent_runs",
    "approve_agent_run",
    "pause_agent_run",
    "import_agent_candidates",
    "download_agent_fulltext",
    "apply_agent_reading_card",
    "export_agent_report",
    "cancel_agent_run",
]
