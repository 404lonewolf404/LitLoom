"""Research goals, workflow selection, and agent plans."""

from __future__ import annotations

import sqlite3

from ..services import app_services as _services


def _json_value(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = _services().json.loads(value or "")
    except (TypeError, ValueError, _services().json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _goal_json_fields() -> tuple[str, ...]:
    return (
        "scope_json",
        "time_range_json",
        "focus_json",
        "exclusions_json",
        "deliverables_json",
        "success_criteria_json",
        "stop_conditions_json",
        "sources_json",
    )


def research_goal_snapshot(row: sqlite3.Row | dict | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    for field in _services()._goal_json_fields():
        default = {} if field in {"scope_json", "time_range_json"} else []
        item[field.removesuffix("_json")] = _services()._json_value(
            item.pop(field, ""), default
        )
    item["sources"] = [
        source
        for source in item.get("sources", [])
        if source in _services().SOURCE_SEARCH
    ]
    return item


def agent_step_snapshot(row: sqlite3.Row | dict | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    for field in (
        "dependencies_json",
        "input_json",
        "output_json",
        "input_schema_json",
        "output_schema_json",
        "completion_json",
        "failure_policy_json",
        "budget_json",
    ):
        default = [] if field == "dependencies_json" else {}
        item[field.removesuffix("_json")] = _services()._json_value(
            item.pop(field, ""), default
        )
    item["requires_confirmation"] = bool(item.get("requires_confirmation"))
    return item


def agent_run_snapshot(run_id: str, include_steps: bool = True) -> dict | None:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["plan"] = _services()._json_value(item.pop("plan_json", ""), {})
        item["budget"] = _services()._json_value(item.pop("budget_json", ""), {})
        item["completion_state"] = item["budget"].get("completion_state") or item.get(
            "status"
        )
        goal = db.execute(
            "SELECT * FROM research_goals WHERE id=?", (row["goal_id"],)
        ).fetchone()
        if include_steps:
            steps = [
                _services().agent_step_snapshot(step)
                for step in db.execute(
                    "SELECT * FROM agent_steps WHERE run_id=? ORDER BY ordinal,id",
                    (run_id,),
                )
            ]
            calls = [
                dict(call)
                for call in db.execute(
                    "SELECT id,step_id,tool_name,tool_class,status,error,attempts,started_at,completed_at,duration_ms FROM agent_tool_calls WHERE run_id=? ORDER BY started_at DESC LIMIT 100",
                    (run_id,),
                )
            ]
            artifacts = []
            for artifact in db.execute(
                "SELECT id,step_id,artifact_type,title,version,content_json,created_at FROM agent_artifacts WHERE run_id=? ORDER BY created_at DESC",
                (run_id,),
            ):
                saved = dict(artifact)
                saved["content"] = _services()._json_value(
                    saved.pop("content_json", ""), {}
                )
                artifacts.append(saved)
            item["steps"] = steps
            item["tool_calls"] = calls
            item["artifacts"] = artifacts
            completed_steps = sum(
                1 for step in steps if step.get("status") == "success"
            )
            total_steps = len(steps)
            started_at = str(item.get("started_at") or "")
            completed_at = str(item.get("completed_at") or "")
            elapsed_seconds = 0
            try:
                started_dt = _services().datetime.fromisoformat(started_at)
                ended_dt = (
                    _services().datetime.fromisoformat(completed_at)
                    if completed_at
                    else _services().datetime.now(_services().timezone.utc)
                )
                elapsed_seconds = max(0, round((ended_dt - started_dt).total_seconds()))
            except (TypeError, ValueError):
                pass
            active_step = next(
                (step for step in steps if step.get("status") == "running"), None
            )
            item["progress"] = {
                "completed_steps": completed_steps,
                "total_steps": total_steps,
                "percent": (
                    round(completed_steps / total_steps * 100) if total_steps else 0
                ),
                "elapsed_seconds": elapsed_seconds,
                "active_step_key": (
                    active_step.get("step_key", "") if active_step else ""
                ),
                "active_step_title": (
                    active_step.get("title", "") if active_step else ""
                ),
                "latest_tool": calls[0].get("tool_name", "") if calls else "",
            }
    item["goal"] = _services().research_goal_snapshot(goal)
    return item


def list_research_goals() -> list[dict]:
    with _services().DB_LOCK, _services().db_session() as db:
        rows = db.execute(
            "SELECT * FROM research_goals ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,updated_at DESC"
        ).fetchall()
        goals = [_services().research_goal_snapshot(row) for row in rows]
        for goal in goals:
            latest = db.execute(
                "SELECT id,status,current_step_id,created_at,updated_at,completed_at,error,budget_json FROM agent_runs WHERE goal_id=? ORDER BY created_at DESC LIMIT 1",
                (goal["id"],),
            ).fetchone()
            if latest:
                latest_item = dict(latest)
                latest_item["completion_state"] = _services()._json_value(
                    latest_item.pop("budget_json", ""), {}
                ).get("completion_state") or latest_item.get("status")
                goal["latest_run"] = latest_item
            else:
                goal["latest_run"] = None
            goal["memory"] = {
                "papers": int(
                    db.execute(
                        "SELECT COUNT(*) FROM research_goal_papers WHERE goal_id=?",
                        (goal["id"],),
                    ).fetchone()[0]
                ),
                "new_papers": int(
                    db.execute(
                        "SELECT COUNT(*) FROM research_goal_papers WHERE goal_id=? AND user_decision='new'",
                        (goal["id"],),
                    ).fetchone()[0]
                ),
                "claims": int(
                    db.execute(
                        "SELECT COUNT(*) FROM research_claims WHERE goal_id=?",
                        (goal["id"],),
                    ).fetchone()[0]
                ),
                "open_critiques": int(
                    db.execute(
                        "SELECT COUNT(*) FROM claim_critiques WHERE goal_id=? AND status='open'",
                        (goal["id"],),
                    ).fetchone()[0]
                ),
            }
    return goals


def select_agent_workflow(
    objective: str, requested: str = "research_map", auto_run: bool = False
) -> dict:
    """Route a goal to the smallest workflow that fits its stated intent."""
    if requested in _services().AGENT_WORKFLOWS:
        if auto_run and requested != "follow_up":
            raise ValueError("定期自动运行仅支持前沿追踪工作流，避免重复执行全文分析")
        return {
            "mode": "manual",
            "requested": requested,
            "selected": requested,
            "confidence": "explicit",
            "signals": [],
            "reason": "按你指定的工作流执行。",
        }
    if requested != "auto":
        raise ValueError("不支持的 Agent 工作流")

    text = _services().clean_text(objective, 12000).casefold()
    evidence_terms = [
        "精读",
        "全文",
        "页码证据",
        "证据核验",
        "逐篇分析",
        "逐篇比较",
        "实验细节",
        "复现",
        "批判性分析",
        "evidence review",
        "full-text",
        "reproducibility",
    ]
    follow_up_terms = [
        "前沿",
        "最新进展",
        "最新论文",
        "最新研究",
        "持续跟踪",
        "持续追踪",
        "跟踪",
        "追踪",
        "新增论文",
        "近期工作",
        "monitor",
        "follow-up",
        "recent papers",
        "new papers",
    ]
    evidence_signals = [term for term in evidence_terms if term in text]
    follow_up_signals = [term for term in follow_up_terms if term in text]
    if auto_run:
        return {
            "mode": "auto",
            "requested": "auto",
            "selected": "follow_up",
            "confidence": "high",
            "signals": ["定期自动运行"],
            "reason": "定期任务使用前沿追踪流程，只比较新增论文和版本变化，不重复执行全文精读。",
        }
    if evidence_signals:
        return {
            "mode": "auto",
            "requested": "auto",
            "selected": "evidence_review",
            "confidence": "high",
            "signals": evidence_signals[:6],
            "reason": "识别到全文、精读或证据核验需求，优先检查本地 PDF 并形成可回溯证据。",
        }
    if follow_up_signals:
        return {
            "mode": "auto",
            "requested": "auto",
            "selected": "follow_up",
            "confidence": "high",
            "signals": follow_up_signals[:6],
            "reason": "识别到前沿或新增工作追踪需求，使用长期研究记忆区分新增、变化和重复论文。",
        }
    return {
        "mode": "auto",
        "requested": "auto",
        "selected": "research_map",
        "confidence": "default",
        "signals": [],
        "reason": "没有明确的全文核验或前沿追踪信号，默认梳理领域结构、方法、证据缺口和阅读顺序。",
    }


def create_research_goal(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("研究目标必须是 JSON 对象")
    objective = _services().clean_text(data.get("objective", ""), 12000)
    if len(objective) < 8:
        raise ValueError("研究目标至少需要 8 个字符")
    name = _services().clean_text(data.get("name", ""), 240) or objective[:80]
    requested_workflow = (
        _services().clean_text(data.get("workflow", "research_map"), 60)
        or "research_map"
    )
    auto_run_value = data.get("auto_run", False)
    auto_run = auto_run_value is True or str(auto_run_value).casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cadence_hours = min(max(int(data.get("cadence_hours", 168)), 24), 8760)
    selection = _services().select_agent_workflow(
        objective, requested_workflow, auto_run
    )
    workflow = selection["selected"]
    raw_sources = data.get("sources", ["arxiv", "openalex", "crossref"])
    if not isinstance(raw_sources, list):
        raise ValueError("sources 必须是列表")
    sources = list(
        dict.fromkeys(
            source for source in raw_sources if source in _services().SOURCE_SEARCH
        )
    )
    if not sources:
        raise ValueError("至少选择一个有效检索来源")

    def list_value(key, default=None, limit=40):
        value = data.get(key, default or [])
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, list):
            raise ValueError(f"{key} 必须是列表或逗号分隔文本")
        return list(
            dict.fromkeys(
                _services().clean_text(item, 500)
                for item in value
                if _services().clean_text(item, 500)
            )
        )[:limit]

    now = _services().now_iso()
    goal_id = str(_services().uuid.uuid4())
    scope = dict(data.get("scope")) if isinstance(data.get("scope"), dict) else {}
    scope["workflow_selection"] = selection
    values = {
        "id": goal_id,
        "name": name,
        "objective": objective,
        "core_question": _services().clean_text(
            data.get("core_question", objective), 5000
        ),
        "scope_json": _services().json.dumps(scope, ensure_ascii=False),
        "time_range_json": _services().json.dumps(
            data.get("time_range") if isinstance(data.get("time_range"), dict) else {},
            ensure_ascii=False,
        ),
        "focus_json": _services().json.dumps(list_value("focus"), ensure_ascii=False),
        "exclusions_json": _services().json.dumps(
            list_value("exclusions"), ensure_ascii=False
        ),
        "deliverables_json": _services().json.dumps(
            list_value("deliverables", ["研究地图", "阅读路线"]), ensure_ascii=False
        ),
        "success_criteria_json": _services().json.dumps(
            list_value("success_criteria"), ensure_ascii=False
        ),
        "stop_conditions_json": _services().json.dumps(
            list_value("stop_conditions"), ensure_ascii=False
        ),
        "sources_json": _services().json.dumps(sources, ensure_ascii=False),
        "workflow": workflow,
        "status": "draft",
        "current_stage": "draft",
        "created_at": now,
        "updated_at": now,
        "cadence_hours": cadence_hours,
        "auto_run": int(auto_run),
        "next_run_at": (
            (
                _services().datetime.now(_services().timezone.utc)
                + _services().timedelta(hours=cadence_hours)
            )
            .replace(microsecond=0)
            .isoformat()
            if auto_run
            else ""
        ),
    }
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            """INSERT INTO research_goals(id,name,objective,core_question,scope_json,time_range_json,focus_json,exclusions_json,deliverables_json,success_criteria_json,stop_conditions_json,sources_json,workflow,status,current_stage,created_at,updated_at,cadence_hours,auto_run,next_run_at)
                    VALUES(:id,:name,:objective,:core_question,:scope_json,:time_range_json,:focus_json,:exclusions_json,:deliverables_json,:success_criteria_json,:stop_conditions_json,:sources_json,:workflow,:status,:current_stage,:created_at,:updated_at,:cadence_hours,:auto_run,:next_run_at)""",
            values,
        )
        _services().record_audit(
            db,
            "agent.goal_created",
            "research_goal",
            goal_id,
            {
                "workflow": workflow,
                "workflow_mode": selection["mode"],
                "auto_run": auto_run,
            },
        )
        row = db.execute(
            "SELECT * FROM research_goals WHERE id=?", (goal_id,)
        ).fetchone()
    return _services().research_goal_snapshot(row)


def get_research_goal(goal_id: str) -> dict | None:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT * FROM research_goals WHERE id=?", (goal_id,)
        ).fetchone()
    return _services().research_goal_snapshot(row)


def build_agent_plan(goal: dict) -> dict:
    workflow = goal.get("workflow", "research_map")
    if workflow == "autonomous":
        return {
            "workflow": "autonomous",
            "workflow_selection": (goal.get("scope") or {}).get(
                "workflow_selection", {}
            ),
            "version": "langgraph-react-v1",
            "execution_mode": "model_selected_tools",
            "description": (
                "LangGraph ReAct 循环：模型读取目标和工具结果后动态选择下一工具，"
                "可继续检索、改用其他证据工具或结束；每轮基于当前证据按需选择，逐步逼近研究目标。"
            ),
            "max_steps": 8,
            "available_tools": _services().tool_catalog(),
            "steps": [],
            "autonomy": {
                "read": "model_selected",
                "draft": "model_selected",
                "apply": "confirmation_required",
                "external": "confirmation_required",
            },
        }

    def step(
        step_key: str,
        agent_type: str,
        title: str,
        tool_name: str,
        tool_class: str,
        dependencies: list[str],
        *,
        input_schema: dict | None = None,
        output_schema: dict | None = None,
        completion: dict | None = None,
        failure_policy: dict | None = None,
        budget: dict | None = None,
    ) -> dict:
        return {
            "step_key": step_key,
            "agent_type": agent_type,
            "title": title,
            "tool_name": tool_name,
            "tool_class": tool_class,
            # Keep the persisted plan aligned with the public tool contract.
            # Some Draft tools (for example full-text multimodal analysis)
            # still require explicit consent because they send user-selected
            # paper content to an external model.
            "requires_confirmation": bool(
                (_services().AGENT_TOOL_SPECS.get(tool_name) or {}).get(
                    "requires_confirmation"
                )
                or tool_class in {"Apply", "External"}
            ),
            "dependencies": dependencies,
            "input_schema": input_schema or {"goal_id": "string", "previous": "object"},
            "output_schema": output_schema or {"status": "string", "data": "object"},
            "completion": completion or {"required": ["tool_success"], "min_items": 0},
            "failure_policy": failure_policy
            or {"retryable": True, "max_attempts": 2, "on_exhausted": "partial"},
            "budget": budget or {"timeout_seconds": 180, "max_items": 50},
        }

    common = [
        step(
            "interpret_goal",
            "PlannerAgent",
            "理解研究目标与覆盖维度",
            "build_search_plan",
            "Draft",
            [],
            output_schema={
                "query": "string",
                "query_variants": "array",
                "coverage_layers": "array",
            },
            completion={"required": ["query", "query_variants"]},
            budget={"timeout_seconds": 60, "max_items": 12},
        ),
        step(
            "search_local_context",
            "SearchAgent",
            "检索本地馆藏和既有阅读证据",
            "search_local_library",
            "Read",
            ["interpret_goal"],
            input_schema={"query": "string", "limit": "integer"},
            output_schema={"results": "array", "total": "integer"},
            completion={"required": ["total"]},
            budget={"timeout_seconds": 120, "max_items": 20},
        ),
        step(
            "search_candidates",
            "SearchAgent",
            "多源检索候选论文",
            "search_sources",
            "Read",
            ["interpret_goal"],
            input_schema={"goal_id": "string", "query_plan": "object"},
            output_schema={
                "run_id": "string",
                "papers": "array",
                "candidate_ids": "array",
            },
            completion={"required": ["run_id", "papers"], "min_items": 1},
            budget={"timeout_seconds": 300, "max_items": 50},
        ),
        step(
            "resolve_identity",
            "IdentityAgent",
            "解析 DOI、arXiv 和本地馆藏身份",
            "resolve_paper_identity",
            "Read",
            ["search_candidates"],
            input_schema={"papers": "array"},
            output_schema={
                "papers": "array",
                "resolved_count": "integer",
                "unresolved_count": "integer",
                "match_counts": "object",
            },
            completion={"required": ["papers", "resolved_count", "unresolved_count"]},
            budget={"timeout_seconds": 90, "max_items": 100},
        ),
        step(
            "merge_versions",
            "IdentityAgent",
            "合并预印本、会议版和期刊版关系",
            "merge_paper_versions",
            "Read",
            ["resolve_identity"],
            input_schema={"papers": "array", "identity": "object"},
            output_schema={"groups": "array", "duplicates": "integer"},
            completion={"required": ["groups"]},
            budget={"timeout_seconds": 90, "max_items": 100},
        ),
        step(
            "triage_candidates",
            "TriageAgent",
            "结合本地研究记忆去重、分层和识别研究缺口",
            "rank_candidates",
            "Draft",
            ["merge_versions", "search_local_context"],
            input_schema={
                "papers": "array",
                "version_groups": "array",
                "local_context": "object",
            },
            output_schema={
                "candidates": "array",
                "high_relevance": "integer",
                "excluded": "integer",
            },
            completion={"required": ["candidates"]},
            budget={"timeout_seconds": 120, "max_items": 50},
        ),
        step(
            "evaluate_coverage",
            "CoverageAgent",
            "评估研究问题覆盖和证据缺口",
            "evaluate_coverage",
            "Draft",
            ["triage_candidates"],
            input_schema={"goal": "object", "triage": "object"},
            output_schema={
                "coverage": "object",
                "gaps": "array",
                "needs_expansion": "boolean",
            },
            completion={"required": ["coverage", "needs_expansion"]},
            budget={"timeout_seconds": 90, "max_items": 30},
        ),
        step(
            "expand_search",
            "SearchAgent",
            "针对缺口进行一次补充检索",
            "expand_search",
            "Read",
            ["evaluate_coverage"],
            input_schema={"goal": "object", "coverage": "object", "previous": "object"},
            output_schema={
                "expanded": "boolean",
                "papers": "array",
                "run_id": "string",
            },
            completion={"required": ["expanded"]},
            budget={"timeout_seconds": 300, "max_items": 30},
        ),
        step(
            "expand_citations",
            "SearchAgent",
            "沿已导入种子的引用、被引和相似关系扩展一轮",
            "expand_citations",
            "Read",
            ["expand_search"],
            input_schema={"goal": "object", "papers": "array", "limit": "integer"},
            output_schema={
                "status": "string",
                "expanded": "boolean",
                "papers": "array",
                "candidate_ids": "array",
                "history": "object",
            },
            completion={"required": ["status", "expanded"]},
            budget={"timeout_seconds": 180, "max_items": 60},
        ),
    ]
    if workflow == "research_map":
        common.extend(
            [
                step(
                    "extract_claims",
                    "ExtractionAgent",
                    "按方法、数据、结果和局限提取 Claim",
                    "extract_claims",
                    "Draft",
                    ["expand_citations"],
                    output_schema={"claims": "array", "claim_count": "integer"},
                    completion={"required": ["claim_count"]},
                ),
                step(
                    "verify_evidence",
                    "VerifierAgent",
                    "核验摘要引文并标记全文缺口",
                    "verify_evidence",
                    "Read",
                    ["extract_claims"],
                    output_schema={"results": "array", "needs_fulltext": "integer"},
                    completion={"required": ["claims_checked"]},
                ),
                step(
                    "critique_claims",
                    "CriticAgent",
                    "寻找反例、冲突、偏差和过度外推",
                    "critique_claims",
                    "Draft",
                    ["verify_evidence"],
                    output_schema={
                        "critiques": "array",
                        "open_issues": "array",
                        "critique_count": "integer",
                    },
                    completion={"required": ["critique_count", "status"]},
                ),
                step(
                    "resolve_evidence_gaps",
                    "DocumentAgent",
                    "针对未核验 Claim 补找本地页面证据",
                    "resolve_evidence_gaps",
                    "Read",
                    ["critique_claims"],
                    input_schema={
                        "claims": "array",
                        "verification": "object",
                        "round": "integer",
                    },
                    output_schema={
                        "attempted": "integer",
                        "resolved_count": "integer",
                        "remaining_count": "integer",
                        "status": "string",
                    },
                    completion={
                        "required": [
                            "attempted",
                            "resolved_count",
                            "remaining_count",
                            "status",
                        ]
                    },
                    budget={"timeout_seconds": 180, "max_items": 30},
                ),
                step(
                    "verify_evidence_refined",
                    "VerifierAgent",
                    "重新核验补证据后的 Claim",
                    "verify_evidence",
                    "Read",
                    ["resolve_evidence_gaps"],
                    output_schema={
                        "results": "array",
                        "needs_fulltext": "integer",
                        "claims_checked": "integer",
                    },
                    completion={"required": ["claims_checked"]},
                ),
                step(
                    "critique_claims_refined",
                    "CriticAgent",
                    "基于第二轮证据重新检查风险",
                    "critique_claims",
                    "Draft",
                    ["verify_evidence_refined"],
                    input_schema={"claims": "array", "verification": "object"},
                    output_schema={
                        "critiques": "array",
                        "open_issues": "array",
                        "critique_count": "integer",
                    },
                    completion={"required": ["critique_count", "status"]},
                ),
                step(
                    "compare_candidates",
                    "SynthesisAgent",
                    "按统一维度比较候选论文",
                    "compare_papers",
                    "Draft",
                    ["critique_claims_refined"],
                    input_schema={"papers": "array", "claims": "array"},
                    output_schema={
                        "dimensions": "array",
                        "rows": "array",
                        "gaps": "array",
                        "row_count": "integer",
                    },
                    completion={"required": ["row_count"]},
                    budget={"timeout_seconds": 120, "max_items": 50},
                ),
                step(
                    "resolve_comparison_gaps",
                    "DocumentAgent",
                    "针对比较缺字段回到本地页面补证据",
                    "resolve_comparison_gaps",
                    "Read",
                    ["compare_candidates"],
                    input_schema={
                        "papers": "array",
                        "claims": "array",
                        "comparison": "object",
                    },
                    output_schema={
                        "attempted": "integer",
                        "resolved_count": "integer",
                        "remaining_count": "integer",
                        "claims": "array",
                        "status": "string",
                    },
                    completion={
                        "required": [
                            "attempted",
                            "resolved_count",
                            "remaining_count",
                            "status",
                        ]
                    },
                    budget={"timeout_seconds": 180, "max_items": 30},
                ),
                step(
                    "refresh_comparison",
                    "SynthesisAgent",
                    "合并补证据 Claim 后重建最终比较矩阵",
                    "compare_papers",
                    "Draft",
                    ["resolve_comparison_gaps"],
                    input_schema={"papers": "array", "claims": "array"},
                    output_schema={
                        "dimensions": "array",
                        "rows": "array",
                        "gaps": "array",
                        "row_count": "integer",
                    },
                    completion={"required": ["row_count"]},
                    budget={"timeout_seconds": 120, "max_items": 50},
                ),
                step(
                    "verify_comparison_evidence",
                    "VerifierAgent",
                    "核验比较矩阵新增页级证据",
                    "verify_evidence",
                    "Read",
                    ["refresh_comparison"],
                    output_schema={
                        "results": "array",
                        "needs_fulltext": "integer",
                        "claims_checked": "integer",
                    },
                    completion={"required": ["claims_checked"]},
                ),
                step(
                    "critique_claims_final",
                    "CriticAgent",
                    "对最终比较 Claim 执行批判检查",
                    "critique_claims",
                    "Draft",
                    ["verify_comparison_evidence"],
                    input_schema={"claims": "array", "verification": "object"},
                    output_schema={
                        "critiques": "array",
                        "open_issues": "array",
                        "critique_count": "integer",
                    },
                    completion={"required": ["critique_count", "status"]},
                ),
                step(
                    "paper_analysis_overview",
                    "PaperAnalystAgent",
                    "生成单篇论文专业分析总览、证据覆盖与复现行动",
                    "build_paper_analysis_overview",
                    "Read",
                    ["critique_claims_final"],
                    input_schema={
                        "papers": "array",
                        "claims": "array",
                        "verification": "object",
                        "critique": "object",
                    },
                    output_schema={
                        "paper_overviews": "array",
                        "overview_count": "integer",
                        "fulltext_count": "integer",
                        "evidence_count": "integer",
                        "status": "string",
                    },
                    completion={"required": ["overview_count", "status"]},
                    budget={"timeout_seconds": 180, "max_items": 8},
                ),
                step(
                    "reading_route",
                    "SynthesisAgent",
                    "生成基础、方法、最新和补充阅读顺序",
                    "generate_reading_route",
                    "Draft",
                    ["critique_claims_final"],
                    input_schema={"papers": "array", "claims": "array"},
                    output_schema={"route": "array", "route_count": "integer"},
                    completion={"required": ["route_count"]},
                    budget={"timeout_seconds": 90, "max_items": 50},
                ),
                step(
                    "synthesize_map",
                    "SynthesisAgent",
                    "形成研究地图与阅读路线",
                    "generate_research_brief",
                    "Draft",
                    [
                        "critique_claims_final",
                        "reading_route",
                        "paper_analysis_overview",
                    ],
                    output_schema={
                        "artifact_type": "string",
                        "candidates": "array",
                        "claims": "array",
                        "critique": "object",
                    },
                    completion={"required": ["artifact_type", "candidates"]},
                ),
                step(
                    "check_citation_completeness",
                    "VerifierAgent",
                    "检查研究地图中的结论是否都能回到 Claim",
                    "check_citation_completeness",
                    "Read",
                    ["synthesize_map"],
                    output_schema={
                        "claims_total": "integer",
                        "claims_cited": "integer",
                        "missing_claim_ids": "array",
                        "complete": "boolean",
                    },
                    completion={
                        "required": ["claims_total", "claims_cited", "complete"]
                    },
                ),
                step(
                    "evaluate_stop_conditions",
                    "PlannerAgent",
                    "根据覆盖、证据和预算决定是否停止",
                    "evaluate_stop_conditions",
                    "Read",
                    ["check_citation_completeness"],
                    output_schema={
                        "stop": "boolean",
                        "reasons": "array",
                        "blocking_reasons": "array",
                    },
                    completion={"required": ["stop"]},
                ),
            ]
        )
    elif workflow == "evidence_review":
        common.extend(
            [
                step(
                    "prepare_fulltext",
                    "DocumentAgent",
                    "准备全文来源并列出下载缺口",
                    "prepare_fulltext",
                    "Read",
                    ["expand_citations"],
                    input_schema={"papers": "array"},
                    output_schema={
                        "papers": "array",
                        "ready_count": "integer",
                        "downloadable_count": "integer",
                        "missing_count": "integer",
                    },
                    completion={
                        "required": [
                            "ready_count",
                            "downloadable_count",
                            "missing_count",
                        ]
                    },
                    budget={"timeout_seconds": 120, "max_items": 8},
                ),
                step(
                    "extract_evidence",
                    "DocumentAgent",
                    "检查全文与页级证据",
                    "extract_pdf_pages",
                    "Read",
                    ["prepare_fulltext"],
                    input_schema={"paper_ids": "array"},
                    output_schema={
                        "results": "array",
                        "page_count": "integer",
                        "papers_processed": "integer",
                        "skipped": "array",
                    },
                    completion={"required": ["papers_processed"]},
                    budget={"timeout_seconds": 600, "max_items": 8},
                ),
                step(
                    "ocr_pages",
                    "DocumentAgent",
                    "对低质量或空白页面执行可选 OCR",
                    "ocr_pdf_pages",
                    "Read",
                    ["extract_evidence"],
                    input_schema={"paper_ids": "array", "max_pages": "integer"},
                    output_schema={
                        "status": "string",
                        "attempted": "integer",
                        "updated": "integer",
                        "skipped": "integer",
                        "pages": "array",
                    },
                    completion={"required": ["status", "attempted", "updated"]},
                    budget={"timeout_seconds": 600, "max_items": 24},
                ),
                step(
                    "draft_reading_card",
                    "WriterAgent",
                    "生成全文阅读卡与读前知识准备",
                    "draft_reading_card",
                    "Draft",
                    ["ocr_pages"],
                    input_schema={"paper": "object"},
                    output_schema={
                        "status": "string",
                        "paper_id": "string",
                        "reading_card": "object",
                        "prereading_guide": "object",
                        "artifact_id": "string",
                        "job_id": "string",
                    },
                    completion={
                        "required": ["status", "paper_id"],
                        "allowed_values": {"status": ["completed", "unavailable"]},
                    },
                    budget={"timeout_seconds": 900, "max_items": 1},
                ),
                step(
                    "extract_claims",
                    "ExtractionAgent",
                    "从全文按研究问题提取可验证 Claim",
                    "extract_claims",
                    "Draft",
                    ["ocr_pages"],
                    output_schema={"claims": "array", "claim_count": "integer"},
                    completion={"required": ["claim_count"]},
                ),
                step(
                    "verify_evidence",
                    "VerifierAgent",
                    "核验全文页级证据和摘要证据边界",
                    "verify_evidence",
                    "Read",
                    ["extract_claims"],
                    output_schema={"results": "array", "needs_fulltext": "integer"},
                    completion={"required": ["claims_checked"]},
                ),
                step(
                    "critique_claims",
                    "CriticAgent",
                    "分析实验设计缺陷和结论适用边界",
                    "critique_claims",
                    "Draft",
                    ["verify_evidence"],
                    output_schema={
                        "critiques": "array",
                        "open_issues": "array",
                        "critique_count": "integer",
                    },
                    completion={"required": ["critique_count", "status"]},
                ),
                step(
                    "resolve_evidence_gaps",
                    "DocumentAgent",
                    "针对未核验 Claim 补找本地页面证据",
                    "resolve_evidence_gaps",
                    "Read",
                    ["critique_claims"],
                    input_schema={
                        "claims": "array",
                        "verification": "object",
                        "round": "integer",
                    },
                    output_schema={
                        "attempted": "integer",
                        "resolved_count": "integer",
                        "remaining_count": "integer",
                        "status": "string",
                    },
                    completion={
                        "required": [
                            "attempted",
                            "resolved_count",
                            "remaining_count",
                            "status",
                        ]
                    },
                    budget={"timeout_seconds": 180, "max_items": 30},
                ),
                step(
                    "verify_evidence_refined",
                    "VerifierAgent",
                    "重新核验补证据后的 Claim",
                    "verify_evidence",
                    "Read",
                    ["resolve_evidence_gaps"],
                    output_schema={
                        "results": "array",
                        "needs_fulltext": "integer",
                        "claims_checked": "integer",
                    },
                    completion={"required": ["claims_checked"]},
                ),
                step(
                    "critique_claims_refined",
                    "CriticAgent",
                    "基于第二轮证据重新检查风险",
                    "critique_claims",
                    "Draft",
                    ["verify_evidence_refined"],
                    input_schema={"claims": "array", "verification": "object"},
                    output_schema={
                        "critiques": "array",
                        "open_issues": "array",
                        "critique_count": "integer",
                    },
                    completion={"required": ["critique_count", "status"]},
                ),
                step(
                    "compare_candidates",
                    "SynthesisAgent",
                    "按统一维度比较候选论文",
                    "compare_papers",
                    "Draft",
                    ["critique_claims_refined"],
                    input_schema={"papers": "array", "claims": "array"},
                    output_schema={
                        "dimensions": "array",
                        "rows": "array",
                        "gaps": "array",
                        "row_count": "integer",
                    },
                    completion={"required": ["row_count"]},
                    budget={"timeout_seconds": 120, "max_items": 50},
                ),
                step(
                    "resolve_comparison_gaps",
                    "DocumentAgent",
                    "针对比较缺字段回到本地页面补证据",
                    "resolve_comparison_gaps",
                    "Read",
                    ["compare_candidates"],
                    input_schema={
                        "papers": "array",
                        "claims": "array",
                        "comparison": "object",
                    },
                    output_schema={
                        "attempted": "integer",
                        "resolved_count": "integer",
                        "remaining_count": "integer",
                        "claims": "array",
                        "status": "string",
                    },
                    completion={
                        "required": [
                            "attempted",
                            "resolved_count",
                            "remaining_count",
                            "status",
                        ]
                    },
                    budget={"timeout_seconds": 180, "max_items": 30},
                ),
                step(
                    "refresh_comparison",
                    "SynthesisAgent",
                    "合并补证据 Claim 后重建最终比较矩阵",
                    "compare_papers",
                    "Draft",
                    ["resolve_comparison_gaps"],
                    input_schema={"papers": "array", "claims": "array"},
                    output_schema={
                        "dimensions": "array",
                        "rows": "array",
                        "gaps": "array",
                        "row_count": "integer",
                    },
                    completion={"required": ["row_count"]},
                    budget={"timeout_seconds": 120, "max_items": 50},
                ),
                step(
                    "verify_comparison_evidence",
                    "VerifierAgent",
                    "核验比较矩阵新增页级证据",
                    "verify_evidence",
                    "Read",
                    ["refresh_comparison"],
                    output_schema={
                        "results": "array",
                        "needs_fulltext": "integer",
                        "claims_checked": "integer",
                    },
                    completion={"required": ["claims_checked"]},
                ),
                step(
                    "critique_claims_final",
                    "CriticAgent",
                    "对最终比较 Claim 执行批判检查",
                    "critique_claims",
                    "Draft",
                    ["verify_comparison_evidence"],
                    input_schema={"claims": "array", "verification": "object"},
                    output_schema={
                        "critiques": "array",
                        "open_issues": "array",
                        "critique_count": "integer",
                    },
                    completion={"required": ["critique_count", "status"]},
                ),
                step(
                    "paper_analysis_overview",
                    "PaperAnalystAgent",
                    "生成单篇论文专业分析总览、证据覆盖与复现行动",
                    "build_paper_analysis_overview",
                    "Read",
                    ["critique_claims_final"],
                    input_schema={
                        "papers": "array",
                        "claims": "array",
                        "verification": "object",
                        "critique": "object",
                    },
                    output_schema={
                        "paper_overviews": "array",
                        "overview_count": "integer",
                        "fulltext_count": "integer",
                        "evidence_count": "integer",
                        "status": "string",
                    },
                    completion={"required": ["overview_count", "status"]},
                    budget={"timeout_seconds": 180, "max_items": 8},
                ),
                step(
                    "reading_route",
                    "SynthesisAgent",
                    "生成证据综述的阅读顺序",
                    "generate_reading_route",
                    "Draft",
                    ["critique_claims_final"],
                    input_schema={"papers": "array", "claims": "array"},
                    output_schema={"route": "array", "route_count": "integer"},
                    completion={"required": ["route_count"]},
                    budget={"timeout_seconds": 90, "max_items": 50},
                ),
                step(
                    "draft_review",
                    "WriterAgent",
                    "生成带证据边界的综述草稿",
                    "generate_research_brief",
                    "Draft",
                    [
                        "critique_claims_final",
                        "reading_route",
                        "draft_reading_card",
                        "paper_analysis_overview",
                    ],
                    output_schema={
                        "artifact_type": "string",
                        "claims": "array",
                        "critique": "object",
                    },
                    completion={"required": ["artifact_type"]},
                ),
                step(
                    "check_citation_completeness",
                    "VerifierAgent",
                    "检查综述草稿中的结论是否都能回到 Claim",
                    "check_citation_completeness",
                    "Read",
                    ["draft_review"],
                    output_schema={
                        "claims_total": "integer",
                        "claims_cited": "integer",
                        "missing_claim_ids": "array",
                        "complete": "boolean",
                    },
                    completion={
                        "required": ["claims_total", "claims_cited", "complete"]
                    },
                ),
                step(
                    "evaluate_stop_conditions",
                    "PlannerAgent",
                    "根据全文证据和风险决定是否停止",
                    "evaluate_stop_conditions",
                    "Read",
                    ["check_citation_completeness"],
                    output_schema={
                        "stop": "boolean",
                        "reasons": "array",
                        "blocking_reasons": "array",
                    },
                    completion={"required": ["stop"]},
                ),
            ]
        )
    else:
        common.extend(
            [
                step(
                    "follow_up_map",
                    "FollowUpAgent",
                    "识别新增论文、版本变化和后续工作",
                    "track_follow_up",
                    "Draft",
                    ["expand_citations"],
                    output_schema={
                        "artifact_type": "string",
                        "status": "string",
                        "new_papers": "array",
                        "changed_papers": "array",
                        "repeated_papers": "array",
                    },
                    completion={"required": ["artifact_type", "status"]},
                ),
                step(
                    "check_citation_completeness",
                    "VerifierAgent",
                    "检查追踪简报中的结论是否都能回到 Claim",
                    "check_citation_completeness",
                    "Read",
                    ["follow_up_map"],
                    output_schema={
                        "claims_total": "integer",
                        "claims_cited": "integer",
                        "missing_claim_ids": "array",
                        "complete": "boolean",
                    },
                    completion={
                        "required": ["claims_total", "claims_cited", "complete"]
                    },
                ),
                step(
                    "evaluate_stop_conditions",
                    "PlannerAgent",
                    "根据新增量、证据和预算决定是否停止",
                    "evaluate_stop_conditions",
                    "Read",
                    ["check_citation_completeness"],
                    output_schema={
                        "stop": "boolean",
                        "reasons": "array",
                        "blocking_reasons": "array",
                    },
                    completion={"required": ["stop"]},
                ),
            ]
        )
    return {
        "workflow": workflow,
        "workflow_selection": (goal.get("scope") or {}).get("workflow_selection", {}),
        "version": "agent-plan-v2",
        "steps": common,
        "autonomy": {
            "read": "automatic",
            "draft": "automatic",
            "apply": "confirmation_required",
            "external": "confirmation_required",
        },
        "stop_conditions": goal.get("stop_conditions")
        or [
            "候选结果已去重并形成可读产物",
            "关键未验证项已明确标记",
            "补充检索最多执行一轮",
            "达到总时间或工具调用预算立即停止",
        ],
        "generated_at": _services().now_iso(),
    }


def create_agent_run(goal_id: str, trigger: str = "manual") -> dict:
    goal = _services().get_research_goal(goal_id)
    if not goal:
        raise LookupError("研究目标不存在")
    if goal["status"] == "completed":
        raise ValueError("已完成目标如需继续，请先将其恢复为 active")
    if goal["workflow"] == "autonomous":
        settings = _services().model_settings()
        if not settings.get("enabled") or not _services().model_api_key():
            raise ValueError("自主 Agent 需要先启用并配置模型 API")
    plan = _services().build_agent_plan(goal)
    now = _services().now_iso()
    run_id = str(_services().uuid.uuid4())
    # Full-text evidence reviews can legitimately spend several minutes on
    # page extraction, OCR and multimodal synthesis. Keep the outer run budget
    # longer than a single model request while remaining bounded and visible.
    runtime_budget = {
        "research_map": 1800,
        "evidence_review": 3600,
        "follow_up": 1200,
        "autonomous": 1800,
    }.get(goal["workflow"], 1800)
    with _services().DB_LOCK, _services().db_session() as db:
        max_tool_calls = {
            "research_map": 40,
            "evidence_review": 48,
            "follow_up": 30,
            "autonomous": 8,
        }.get(goal["workflow"], 40)
        db.execute(
            "INSERT INTO agent_runs(id,goal_id,workflow,status,trigger,plan_json,budget_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                goal_id,
                goal["workflow"],
                "awaiting_confirmation",
                _services().clean_text(trigger, 60) or "manual",
                _services().json.dumps(plan, ensure_ascii=False),
                _services().json.dumps(
                    {
                        "max_steps": int(plan.get("max_steps") or len(plan["steps"])),
                        "max_tool_calls": max_tool_calls,
                        "max_runtime_seconds": runtime_budget,
                        "max_expansion_rounds": 1,
                        "max_refinement_rounds": 1,
                        "refinement_round": 0,
                        "consent_external": False,
                    },
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )
        for ordinal, spec in enumerate(plan["steps"]):
            step_id = str(_services().uuid.uuid4())
            db.execute(
                """INSERT INTO agent_steps(id,run_id,step_key,agent_type,title,tool_name,tool_class,ordinal,dependencies_json,input_schema_json,output_schema_json,completion_json,failure_policy_json,budget_json,requires_confirmation,max_attempts,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    step_id,
                    run_id,
                    spec["step_key"],
                    spec["agent_type"],
                    spec["title"],
                    spec["tool_name"],
                    spec["tool_class"],
                    ordinal,
                    _services().json.dumps(
                        spec.get("dependencies", []), ensure_ascii=False
                    ),
                    _services().json.dumps(
                        spec.get("input_schema", {}), ensure_ascii=False
                    ),
                    _services().json.dumps(
                        spec.get("output_schema", {}), ensure_ascii=False
                    ),
                    _services().json.dumps(
                        spec.get("completion", {}), ensure_ascii=False
                    ),
                    _services().json.dumps(
                        spec.get("failure_policy", {}), ensure_ascii=False
                    ),
                    _services().json.dumps(spec.get("budget", {}), ensure_ascii=False),
                    int(
                        bool(
                            spec.get("requires_confirmation")
                            or spec["tool_class"] in {"Apply", "External"}
                        )
                    ),
                    int((spec.get("failure_policy") or {}).get("max_attempts", 2)),
                    now,
                    now,
                ),
            )
        db.execute(
            "UPDATE research_goals SET status='active',current_stage='plan_review',last_run_at=?,updated_at=? WHERE id=?",
            (now, now, goal_id),
        )
        _services().record_audit(
            db,
            "agent.run_created",
            "agent_run",
            run_id,
            {"goal_id": goal_id, "workflow": goal["workflow"]},
        )
    return _services().agent_run_snapshot(run_id)


__all__ = [
    "_json_value",
    "_goal_json_fields",
    "research_goal_snapshot",
    "agent_step_snapshot",
    "agent_run_snapshot",
    "list_research_goals",
    "select_agent_workflow",
    "create_research_goal",
    "get_research_goal",
    "build_agent_plan",
    "create_agent_run",
]