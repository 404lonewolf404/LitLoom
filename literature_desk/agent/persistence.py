"""Durable records for the bounded autonomous Agent loop.

The research-goal workflow already owns the auditable run tables. This module
uses the same tables for the free-form tool chooser, so both Agent surfaces
share progress, tool-call history, artifacts, and restart diagnostics.
"""

from __future__ import annotations

from typing import Any

from ..services import app_services as _services


def _bounded(value: Any, limit: int = 80_000) -> Any:
    try:
        return _services()._bounded_agent_payload(value, limit)
    except Exception:
        return value


def create_run(
    objective: str,
    context: dict[str, Any],
    max_steps: int,
    catalog: list[dict[str, Any]],
    framework: str,
    goal_id: str = "",
) -> dict[str, str]:
    """Create an autonomous run, optionally attached to an existing goal.

    A goal is the durable memory boundary.  Ad-hoc requests still get a small
    synthetic goal so their trace is fully auditable, while callers that pass a
    known ``goal_id`` continue the same memory stream instead of creating an
    unrelated goal for every turn.
    """
    now = _services().now_iso()
    requested_goal_id = str(goal_id or "").strip()
    run_id = str(_services().uuid.uuid4())
    plan = {
        "kind": "autonomous",
        "framework": framework,
        "objective": objective,
        "context": _bounded(context, 12_000),
        "max_steps": max_steps,
        "tool_catalog": catalog,
        # Compatibility alias for traces written by the first autonomous
        # runtime version and for external clients that inspect plan_json.
        "tools": catalog,
        "created_at": now,
    }
    budget = {
        "max_steps": max_steps,
        "max_tool_calls": max_steps,
        "completion_state": "running",
        "consent_external": False,
    }
    with _services().DB_LOCK, _services().db_session() as db:
        if requested_goal_id:
            existing = db.execute(
                "SELECT id FROM research_goals WHERE id=?", (requested_goal_id,)
            ).fetchone()
        else:
            existing = None
        if existing:
            goal_id = requested_goal_id
        else:
            goal_id = str(_services().uuid.uuid4())
            db.execute(
                """INSERT INTO research_goals(
                    id,name,objective,core_question,scope_json,time_range_json,
                    focus_json,exclusions_json,deliverables_json,success_criteria_json,
                    stop_conditions_json,sources_json,workflow,status,current_stage,
                    created_at,updated_at,cadence_hours,auto_run,next_run_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    goal_id,
                    "自主 Agent · " + objective[:72],
                    objective,
                    objective,
                    _services().json.dumps({"kind": "autonomous", "context": _bounded(context, 12_000)}, ensure_ascii=False),
                    "{}",
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    _services().json.dumps(["arxiv", "openalex", "crossref"], ensure_ascii=False),
                    "autonomous",
                    "active",
                    "autonomous",
                    now,
                    now,
                    0,
                    0,
                    "",
                ),
            )
        db.execute(
            """INSERT INTO agent_runs(
                id,goal_id,workflow,status,trigger,plan_json,budget_json,
                created_at,started_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                goal_id,
                "autonomous",
                "running",
                "autonomous_api",
                _services().json.dumps(plan, ensure_ascii=False),
                _services().json.dumps(budget, ensure_ascii=False),
                now,
                now,
                now,
            ),
        )
        _services().record_audit(
            db,
            "agent.autonomous_started",
            "agent_run",
            run_id,
            {"goal_id": goal_id, "framework": framework, "max_steps": max_steps},
        )
    return {"run_id": run_id, "goal_id": goal_id}


def start_step(
    run_id: str,
    ordinal: int,
    tool_name: str,
    tool_class: str,
    payload: dict[str, Any],
    requires_confirmation: bool = False,
) -> str:
    step_id = str(_services().uuid.uuid4())
    now = _services().now_iso()
    step_key = f"autonomous_{ordinal + 1}_{tool_name}"
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            """INSERT INTO agent_steps(
                id,run_id,step_key,agent_type,title,tool_name,tool_class,ordinal,
                dependencies_json,input_json,input_schema_json,output_schema_json,
                completion_json,failure_policy_json,budget_json,status,
                requires_confirmation,attempts,created_at,started_at,updated_at,message
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                step_id,
                run_id,
                step_key,
                "AutonomousAgent",
                f"自主选择并执行 {tool_name}",
                tool_name,
                tool_class,
                ordinal,
                "[]",
                _services().json.dumps(_bounded(payload), ensure_ascii=False),
                "{}",
                "{}",
                "{}",
                _services().json.dumps({"retryable": True, "max_attempts": 2}, ensure_ascii=False),
                _services().json.dumps({"timeout_seconds": 120}, ensure_ascii=False),
                "running",
                int(requires_confirmation),
                1,
                now,
                now,
                now,
                "开始执行工具",
            ),
        )
        db.execute(
            "UPDATE agent_runs SET current_step_id=?,updated_at=? WHERE id=?",
            (step_id, now, run_id),
        )
    return step_id


def mark_step(
    run_id: str,
    step_id: str,
    *,
    status: str,
    output: Any = None,
    error: str = "",
    message: str = "",
) -> None:
    now = _services().now_iso()
    safe_output = _bounded(output) if output is not None else {}
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            """UPDATE agent_steps SET status=?,output_json=?,error=?,message=?,
                       completed_at=?,updated_at=? WHERE id=? AND run_id=?""",
            (
                status,
                _services().json.dumps(safe_output, ensure_ascii=False),
                _services().clean_text(error, 1200),
                _services().clean_text(message, 500),
                now if status in {"success", "failed", "awaiting_confirmation"} else "",
                now,
                step_id,
                run_id,
            ),
        )


def completed_tool_call(run_id: str, step_id: str) -> Any | None:
    """Return a successful observation so restart recovery cannot repeat it."""
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT output_json FROM agent_tool_calls WHERE run_id=? AND step_id=? AND status='success' ORDER BY started_at DESC LIMIT 1",
            (run_id, step_id),
        ).fetchone()
    if not row:
        return None
    return _services()._json_value(row["output_json"], None)


def add_artifact(
    run_id: str,
    goal_id: str,
    step_id: str,
    artifact_type: str,
    content: Any,
    title: str = "自主 Agent 工具结果",
) -> None:
    now = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            """INSERT INTO agent_artifacts(
                id,goal_id,run_id,step_id,artifact_type,title,content_json,version,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                str(_services().uuid.uuid4()),
                goal_id,
                run_id,
                step_id,
                _services().clean_text(artifact_type, 80),
                _services().clean_text(title, 200),
                _services().json.dumps(_bounded(content), ensure_ascii=False),
                1,
                now,
            ),
        )


def finish_run(
    run_id: str,
    *,
    status: str,
    answer: str = "",
    error: str = "",
    results: Any = None,
) -> None:
    now = _services().now_iso()
    persisted_status = "awaiting_confirmation" if status == "needs_confirmation" else status
    budget_patch = {
        "completion_state": persisted_status,
        "answer": _services().clean_text(answer, 4000),
    }
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute("SELECT budget_json FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        budget = _services()._json_value(row[0] if row else "", {})
        budget.update(budget_patch)
        completed_at = "" if persisted_status == "awaiting_confirmation" else now
        db.execute(
            """UPDATE agent_runs SET status=?,error=?,budget_json=?,current_step_id='',
                       completed_at=?,updated_at=? WHERE id=?""",
            (
                persisted_status,
                _services().clean_text(error, 1200),
                _services().json.dumps(budget, ensure_ascii=False),
                completed_at,
                now,
                run_id,
            ),
        )
        _services().record_audit(
            db,
            "agent.autonomous_finished",
            "agent_run",
            run_id,
            {"status": status, "result_count": len(results) if isinstance(results, list) else 0},
        )


__all__ = [
    "create_run",
    "start_step",
    "mark_step",
    "completed_tool_call",
    "add_artifact",
    "finish_run",
]
