"""Metadata drafting job lifecycle."""

from __future__ import annotations

from pathlib import Path

from ..services import app_services as _services

def _metadata_draft_input(data: dict) -> dict:
    return {
        "title": _services().clean_text(data.get("title", ""), 1000),
        "doi": _services().norm_doi(_services().clean_text(data.get("doi", ""), 300)),
        "citation": _services().clean_text(data.get("citation", ""), 20000),
    }


def metadata_draft_job_snapshot(row) -> dict:
    if not row:
        return {}
    item = dict(row)
    try:
        item["result"] = _services().json.loads(item.pop("result_json", "{}") or "{}")
    except (TypeError, ValueError, _services().json.JSONDecodeError):
        item["result"] = {}
    item.pop("input_json", None)
    item.pop("request_hash", None)
    try:
        started = _services().datetime.fromisoformat(
            item.get("started_at") or item.get("created_at")
        )
        ended = _services().datetime.fromisoformat(
            item.get("completed_at") or _services().now_iso()
        )
        item["duration_ms"] = max(0, round((ended - started).total_seconds() * 1000, 2))
    except (TypeError, ValueError):
        item["duration_ms"] = 0
    return item


def get_metadata_draft_job(job_id: str) -> dict | None:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return _services().metadata_draft_job_snapshot(row) if row else None


def _update_metadata_draft_job(job_id: str, **updates) -> dict:
    allowed = {
        "status",
        "stage",
        "progress_current",
        "progress_total",
        "message",
        "result_json",
        "error",
        "input_json",
        "attempts",
        "started_at",
        "updated_at",
        "completed_at",
    }
    values = {key: value for key, value in updates.items() if key in allowed}
    values.setdefault("updated_at", _services().now_iso())
    assignments = ",".join(f"{key}=?" for key in values)
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            f"UPDATE metadata_draft_jobs SET {assignments} WHERE id=?",
            (*values.values(), job_id),
        )
        row = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return _services().metadata_draft_job_snapshot(row) if row else {}


def run_metadata_draft_job(job_id: str) -> None:
    job = _services().get_metadata_draft_job(job_id)
    if not job or job.get("status") != "queued":
        return
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT input_json FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
    if not row:
        return
    try:
        payload = _services().json.loads(row["input_json"] or "{}")
        _services()._update_metadata_draft_job(
            job_id,
            status="running",
            stage="crossref",
            progress_current=0,
            progress_total=2,
            message="正在查询 DOI 与公开元数据",
            started_at=_services().now_iso(),
            error="",
        )

        def progress(stage, current, total, message):
            _services()._update_metadata_draft_job(
                job_id,
                stage=_services().clean_text(stage, 80),
                progress_current=max(0, int(current or 0)),
                progress_total=max(1, int(total or 1)),
                message=_services().clean_text(message, 300),
            )

        result = _services().complete_paper_metadata_draft(
            payload, progress_callback=progress
        )
        status = "partial" if result.get("partial") else "success"
        result_json = _services().json.dumps(result, ensure_ascii=False)
        _services()._update_metadata_draft_job(
            job_id,
            status=status,
            stage="completed",
            progress_current=2,
            progress_total=2,
            message=(
                "已完成，部分字段可重试" if status == "partial" else "元数据草稿已生成"
            ),
            result_json=result_json,
            input_json=(
                "{}"
                if status == "success"
                else _services().json.dumps(payload, ensure_ascii=False)
            ),
            error=_services().clean_text(result.get("ai_error", ""), 500),
            completed_at=_services().now_iso(),
        )
    except Exception as exc:
        detail = _services().clean_text(
            _services().model_error_detail(exc, _services().model_api_key()), 700
        )
        _services()._update_metadata_draft_job(
            job_id,
            status="failed",
            stage="failed",
            message="元数据识别失败",
            error=detail,
            completed_at=_services().now_iso(),
        )


def _mark_metadata_draft_interrupted_for_shutdown(
    job_id: str, message: str | None = None
) -> None:
    """Persist a retryable state when metadata work cannot be queued.

    A request can arrive after the service has started shutting down but
    before the executor has fully closed.  Treat that narrow race as a normal
    durable interruption instead of leaving a misleading ``queued`` row.
    """
    if message is None:
        message = _services().RUNTIME_SHUTDOWN_MESSAGE
    try:
        _services()._update_metadata_draft_job(
            job_id,
            status="interrupted",
            stage="interrupted",
            message=message,
            error="服务重启时任务未完成",
            completed_at=_services().now_iso(),
        )
    except Exception:
        pass


def _enqueue_metadata_draft_job(job_id: str) -> None:
    """Submit metadata work with the same shutdown guarantees as AI jobs."""
    if _services().STOP_EVENT.is_set():
        _services()._mark_metadata_draft_interrupted_for_shutdown(job_id)
        raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
    with _services().METADATA_DRAFT_EXECUTOR_LOCK:
        if _services().STOP_EVENT.is_set():
            _services()._mark_metadata_draft_interrupted_for_shutdown(job_id)
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE)
        if _services().METADATA_DRAFT_EXECUTOR is None:
            _services().METADATA_DRAFT_EXECUTOR = _services().ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="metadata-draft"
            )
        executor = _services().METADATA_DRAFT_EXECUTOR
        try:
            executor.submit(_services().run_metadata_draft_job, job_id)
        except RuntimeError as exc:
            if _services().METADATA_DRAFT_EXECUTOR is executor:
                _services().METADATA_DRAFT_EXECUTOR = None
            _services()._mark_metadata_draft_interrupted_for_shutdown(job_id)
            raise RuntimeError(_services().RUNTIME_SHUTDOWN_MESSAGE) from exc


def submit_metadata_draft_job(data: dict) -> dict:
    payload = _services()._metadata_draft_input(data)
    if not any(payload.values()):
        raise ValueError("请至少填写论文标题、DOI 或引用文本")
    settings = _services().resolve_model_route(_services().model_settings(), "fast")
    identity = {
        "input": payload,
        "model": settings.get("model", ""),
        "protocol": settings.get("protocol", ""),
        "endpoint": _services().model_endpoint(settings),
        "prompt_version": "metadata-draft-v2",
    }
    request_hash = (
        _services()
        .hashlib.sha256(
            _services()
            .json.dumps(identity, ensure_ascii=False, sort_keys=True)
            .encode("utf-8")
        )
        .hexdigest()
    )
    with _services().DB_LOCK, _services().db_session() as db:
        previous = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE request_hash=? ORDER BY created_at DESC LIMIT 1",
            (request_hash,),
        ).fetchone()
        if previous and previous["status"] in {"queued", "running"}:
            return {
                "job": _services().metadata_draft_job_snapshot(previous),
                "deduplicated": True,
            }
        if previous and previous["status"] in {
            "success",
            "partial",
            "failed",
            "interrupted",
        }:
            return {
                "job": _services().metadata_draft_job_snapshot(previous),
                "deduplicated": True,
                "reused": previous["status"] in {"success", "partial"},
            }
        job_id = str(_services().uuid.uuid4())
        created = _services().now_iso()
        db.execute(
            "INSERT INTO metadata_draft_jobs(id,request_hash,status,stage,message,input_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                job_id,
                request_hash,
                "queued",
                "queued",
                "已加入元数据识别队列",
                _services().json.dumps(payload, ensure_ascii=False),
                created,
                created,
            ),
        )
        row = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
    _services()._enqueue_metadata_draft_job(job_id)
    return {"job": _services().metadata_draft_job_snapshot(row), "deduplicated": False}


def retry_metadata_draft_job(job_id: str) -> dict:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row:
            raise LookupError("元数据识别任务不存在")
        if row["status"] in {"queued", "running"}:
            return {
                "job": _services().metadata_draft_job_snapshot(row),
                "deduplicated": True,
            }
        if row["status"] not in {"partial", "failed", "interrupted"}:
            return {
                "job": _services().metadata_draft_job_snapshot(row),
                "deduplicated": True,
            }
        payload = _services().json.loads(row["input_json"] or "{}")
        if not any(payload.values()):
            raise ValueError("任务没有可重试的原始输入，请重新提交标题、DOI 或引用文本")
        db.execute(
            "UPDATE metadata_draft_jobs SET status='queued',stage='queued',progress_current=0,message='已加入重试队列',error='',attempts=attempts+1,started_at='',updated_at=?,completed_at='' WHERE id=?",
            (_services().now_iso(), job_id),
        )
        updated = db.execute(
            "SELECT * FROM metadata_draft_jobs WHERE id=?", (job_id,)
        ).fetchone()
    _services()._enqueue_metadata_draft_job(job_id)
    return {
        "job": _services().metadata_draft_job_snapshot(updated),
        "deduplicated": False,
    }


def resume_metadata_draft_jobs() -> None:
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT id FROM metadata_draft_jobs WHERE status IN ('queued','running') OR (status='interrupted' AND error='服务重启时任务未完成') ORDER BY created_at"
        ).fetchall()
        db.execute(
            "UPDATE metadata_draft_jobs SET status='queued',stage='queued',message='服务启动后继续处理任务',error='',started_at='',updated_at=? WHERE status IN ('queued','running') OR (status='interrupted' AND error='服务重启时任务未完成')",
            (_services().now_iso(),),
        )
    for row in rows:
        try:
            _services()._enqueue_metadata_draft_job(row["id"])
        except RuntimeError:
            # A shutdown can race startup recovery.  The row is already
            # marked interrupted by the enqueue helper and can be retried on
            # the next process start.
            continue


