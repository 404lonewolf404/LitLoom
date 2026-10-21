"""Research-goal planning, agent tools, and workflow execution."""

from __future__ import annotations
import sqlite3

from .services import app_services as _services


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
    }.get(goal["workflow"], 1800)
    with _services().DB_LOCK, _services().db_session() as db:
        max_tool_calls = {
            "research_map": 40,
            "evidence_review": 48,
            "follow_up": 30,
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
                        "max_steps": len(plan["steps"]),
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


def _agent_step_output(db: sqlite3.Connection, run_id: str, step_key: str) -> dict:
    row = db.execute(
        "SELECT output_json FROM agent_steps WHERE run_id=? AND step_key=?",
        (run_id, step_key),
    ).fetchone()
    return _services()._json_value(row[0] if row else "", {})


def _bounded_agent_payload(value, max_chars: int = 350000):
    try:
        serialized = _services().json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"value": _services().clean_text(value, max_chars)}
    if len(serialized) <= max_chars:
        return value
    return {
        "truncated": True,
        "preview": serialized[:max_chars],
        "original_chars": len(serialized),
    }


def _agent_reduced_paper(item: dict) -> dict:
    reduced = {
        key: item.get(key, "")
        for key in (
            "id",
            "title",
            "authors",
            "year",
            "venue",
            "doi",
            "arxiv_id",
            "openalex_id",
            "external_id",
            "url",
            "pdf_url",
            "source",
            "status",
            "topic",
            "relevance_score",
            "relevance_label",
            "relevance_reasons",
            "discovery_score",
            "discovery_reasons",
            "seen_before",
            "open_access",
            "citation_count",
            "agent_relevance_class",
            "agent_relevance_score",
            "quality_filter_reason",
        )
    }
    reduced["abstract"] = _services().clean_text(item.get("abstract", ""), 1600)
    return reduced


def _agent_resolve_paper_identities(papers: list[dict], limit: int = 100) -> dict:
    """Resolve candidate identities without importing or mutating papers.

    Discovery providers frequently return the same work with different
    transient IDs.  This step deliberately only reads the local library and
    annotates the snapshot, leaving the later version merger responsible for
    grouping editions.  Keeping the status explicit lets a long-running
    follow-up distinguish an exact match from a title-only or unresolved hit.
    """
    items = [item for item in papers if isinstance(item, dict) and item.get("title")][
        :limit
    ]
    resolved = []
    counts = {"doi": 0, "arxiv": 0, "title": 0, "unresolved": 0}
    with _services().DB_LOCK, _services().db_session() as db:
        for raw in items:
            item = _services()._agent_reduced_paper(raw)
            doi = _services().norm_doi(item.get("doi", ""))
            arxiv_id = _services().norm_arxiv(item.get("arxiv_id", ""))
            title = _services().title_key(item.get("title", ""))
            row = None
            status = "unresolved"
            if doi:
                row = db.execute(
                    "SELECT * FROM papers WHERE doi=? LIMIT 1", (doi,)
                ).fetchone()
                if row:
                    status = "doi"
            if not row and arxiv_id:
                row = db.execute(
                    "SELECT * FROM papers WHERE arxiv_id=? LIMIT 1", (arxiv_id,)
                ).fetchone()
                if row:
                    status = "arxiv"
            if not row and title:
                row = db.execute(
                    "SELECT * FROM papers WHERE title_key=? AND (year=? OR ? IS NULL) LIMIT 1",
                    (title, item.get("year"), item.get("year")),
                ).fetchone()
                if row:
                    status = "title"
            counts[status] += 1
            if row:
                local = dict(row)
                item["local_paper_id"] = local.get("id", "")
                item["resolved_title"] = local.get("title", "")
                item["has_local_pdf"] = bool(
                    db.execute(
                        "SELECT 1 FROM attachments WHERE paper_id=? LIMIT 1",
                        (local["id"],),
                    ).fetchone()
                )
                item["local_document_pages"] = int(
                    db.execute(
                        "SELECT COUNT(*) FROM document_pages WHERE paper_id=?",
                        (local["id"],),
                    ).fetchone()[0]
                )
                # Preserve the strongest stable identity available locally.
                item["resolved_identity"] = _services().candidate_identity(local)
            else:
                item["local_paper_id"] = ""
                item["resolved_identity"] = _services().candidate_identity(item)
            item["identity_status"] = status
            resolved.append(item)
    return {
        "papers": resolved,
        "resolved_count": sum(
            value for key, value in counts.items() if key != "unresolved"
        ),
        "unresolved_count": counts["unresolved"],
        "match_counts": counts,
        "status": (
            "resolved"
            if resolved and counts["unresolved"] == 0
            else ("partial" if resolved else "empty")
        ),
    }


def _agent_focus_papers(candidates: list[dict], limit: int = 3) -> list[dict]:
    """Choose a small, explainable focus set instead of always taking index 0.

    Full-text work is expensive. Prefer a local PDF, then relevance and
    novelty, while preserving the original ranking as a final tie-breaker.
    Every selected record carries a human-readable reason for the UI/audit.
    """
    usable = [
        item for item in candidates if isinstance(item, dict) and item.get("title")
    ]
    ranked = sorted(
        enumerate(usable),
        key=lambda pair: (
            bool(pair[1].get("has_local_pdf")),
            bool(pair[1].get("local_document_pages")),
            float(
                pair[1].get("relevance_score") or pair[1].get("discovery_score") or 0
            ),
            int(pair[1].get("year") or 0),
            -pair[0],
        ),
        reverse=True,
    )
    selected = []
    for rank, (_index, item) in enumerate(ranked[: max(1, min(int(limit), 8))], 1):
        copy = dict(item)
        reasons = []
        if copy.get("has_local_pdf"):
            reasons.append("已有本地 PDF，可直接做页级核验")
        if copy.get("local_document_pages"):
            reasons.append("已有页级文本，能复用历史提取结果")
        if copy.get("relevance_label"):
            reasons.append(str(copy["relevance_label"]))
        if not reasons:
            reasons.append("相关性与时间排序靠前")
        copy["focus_rank"] = rank
        copy["focus_reason"] = "；".join(reasons)
        selected.append(copy)
    return selected


def _infer_page_section(page_text: str) -> str:
    """Infer a conservative section label from page headings when available."""
    normalized = (
        _services()
        .re.sub(r"\s+", " ", _services().clean_text(page_text, 20000))
        .strip()
    )
    if not normalized:
        return "PDF 页面文本"
    patterns = (
        (r"\babstract\b|摘要", "Abstract"),
        (r"\bintroduction\b|引言|绪论", "Introduction"),
        (r"\brelated work|background\b|相关工作|背景", "Background / Related Work"),
        (r"\bmethod(?:s|ology)?\b|方法|模型架构", "Methods"),
        (r"\bexperiment(?:s)?\b|实验|评测", "Experiments"),
        (r"\bresult(?:s)?\b|结果", "Results"),
        (r"\bdiscussion\b|讨论", "Discussion"),
        (r"\bconclusion(?:s)?\b|结论", "Conclusion"),
        (r"\blimitation(?:s)?\b|局限|不足", "Limitations"),
    )
    first_lines = " ".join(
        _services().re.split(r"\n+", _services().clean_text(page_text, 6000))[:8]
    )
    for pattern, label in patterns:
        if _services().re.search(pattern, first_lines, _services().re.I):
            return label
    return "PDF 页面文本"


def _agent_paper_key(item: dict) -> str:
    """Return the stable identity used by a research goal's long memory."""
    return _services().candidate_identity(item)


def _unique_agent_papers(*groups: list[dict] | None, limit: int = 100) -> list[dict]:
    """Merge candidate snapshots while preserving one record per stable work.

    A supplementary search intentionally gets a fresh candidate ID.  The
    identity must therefore come from DOI/arXiv/title rather than the
    transient ID, and the first record keeps its ranking while later records
    only fill missing metadata.
    """
    merged: dict[str, dict] = {}
    for group in groups:
        if not isinstance(group, list):
            continue
        for raw in group:
            if not isinstance(raw, dict) or not raw.get("title"):
                continue
            item = dict(raw)
            key = (
                _services()._agent_paper_key(item)
                or f"id:{_services().clean_text(item.get('id', ''), 200)}"
            )
            if key not in merged:
                merged[key] = item
                if len(merged) >= limit:
                    return list(merged.values())
                continue
            existing = merged[key]
            for field, value in item.items():
                if existing.get(field) in (None, "", [], {}):
                    existing[field] = value
    return list(merged.values())


def _unique_agent_claims(*groups: list[dict] | None, limit: int = 200) -> list[dict]:
    """Merge extracted and follow-up claims without losing their provenance."""
    merged: dict[str, dict] = {}
    for group in groups:
        if not isinstance(group, list):
            continue
        for raw in group:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            claim_id = _services().clean_text(
                item.get("id") or item.get("claim_id"), 120
            )
            fingerprint = "|".join(
                _services().clean_text(item.get(field, ""), 1200).casefold()
                for field in ("paper_id", "claim_type", "claim_text", "page")
            )
            key = claim_id or (
                "text:"
                + _services().hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
            )
            if key not in merged:
                merged[key] = item
                if len(merged) >= limit:
                    return list(merged.values())
    return list(merged.values())


def _record_goal_paper_history(
    db: sqlite3.Connection, goal_id: str, run_id: str, papers: list[dict]
) -> dict:
    """Persist discovery history without importing candidates into the library.

    A goal remembers that a work was seen, while the normal candidate inbox
    still requires an explicit user import.  DOI/arXiv identities are stable
    across providers and title/year is used only as a conservative fallback.
    """
    now = _services().now_iso()
    new_items, changed_items, repeated_items = [], [], []
    for raw in papers[:100]:
        if not isinstance(raw, dict) or not _services().clean_text(
            raw.get("title", ""), 1000
        ):
            continue
        key = _services()._agent_paper_key(raw)
        doi = _services().norm_doi(raw.get("doi", ""))
        arxiv_id = _services().norm_arxiv(raw.get("arxiv_id", ""))
        version_key = (
            ("doi:" + doi)
            if doi
            else (
                ("arxiv:" + arxiv_id)
                if arxiv_id
                else "title:" + _services().title_key(raw.get("title", ""))
            )
        )
        local_id = _services().clean_text(
            raw.get("local_paper_id") or raw.get("id", ""), 200
        )
        existing = db.execute(
            "SELECT * FROM research_goal_papers WHERE goal_id=? AND paper_key=?",
            (goal_id, key),
        ).fetchone()
        metadata = {
            field: raw.get(field)
            for field in (
                "year",
                "venue",
                "source",
                "pdf_url",
                "url",
                "openalex_id",
                "external_id",
                "relevance_label",
                "discovery_score",
            )
            if raw.get(field) not in (None, "")
        }
        if existing:
            changed = any(
                _services().clean_text(existing[field], 2000)
                != _services().clean_text(raw.get(field, ""), 2000)
                for field in ("title", "doi", "arxiv_id")
            )
            db.execute(
                """UPDATE research_goal_papers SET local_paper_id=CASE WHEN ?<>'' THEN ? ELSE local_paper_id END,title=CASE WHEN ?<>'' THEN ? ELSE title END,doi=CASE WHEN ?<>'' THEN ? ELSE doi END,arxiv_id=CASE WHEN ?<>'' THEN ? ELSE arxiv_id END,version_key=?,last_seen_at=?,seen_count=seen_count+1,last_run_id=?,change_summary=?,metadata_json=? WHERE goal_id=? AND paper_key=?""",
                (
                    local_id,
                    local_id,
                    _services().clean_text(raw.get("title", ""), 1000),
                    _services().clean_text(raw.get("title", ""), 1000),
                    doi,
                    doi,
                    arxiv_id,
                    arxiv_id,
                    version_key,
                    now,
                    run_id,
                    "元数据发生变化" if changed else "",
                    _services().json.dumps(metadata, ensure_ascii=False),
                    goal_id,
                    key,
                ),
            )
            (changed_items if changed else repeated_items).append(
                {
                    "paper_key": key,
                    "title": raw.get("title", ""),
                    "change_summary": "元数据发生变化" if changed else "",
                }
            )
        else:
            db.execute(
                """INSERT INTO research_goal_papers(goal_id,paper_key,local_paper_id,title,doi,arxiv_id,version_key,first_seen_at,last_seen_at,seen_count,first_run_id,last_run_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    goal_id,
                    key,
                    local_id,
                    _services().clean_text(raw.get("title", ""), 1000),
                    doi,
                    arxiv_id,
                    version_key,
                    now,
                    now,
                    1,
                    run_id,
                    run_id,
                    _services().json.dumps(metadata, ensure_ascii=False),
                ),
            )
            new_items.append(
                {
                    "paper_key": key,
                    "title": raw.get("title", ""),
                    "change_summary": "首次发现",
                }
            )
    return {
        "new": new_items,
        "changed": changed_items,
        "repeated": repeated_items,
        "new_count": len(new_items),
        "changed_count": len(changed_items),
        "repeated_count": len(repeated_items),
    }


def _agent_fulltext_reading_card(paper: dict, payload: dict) -> dict:
    """Run the real page-aware multimodal reading-card pipeline for the Agent.

    Agent drafts are review-only: the persistent AI job writes an artifact but
    does not overwrite the user's reading card. The same progress, cancellation
    and restart semantics used by the UI therefore apply to Agent runs too.
    """
    paper_id = _services().clean_text(
        paper.get("local_paper_id") or paper.get("id", ""), 200
    )
    if not paper_id and not paper.get("title"):
        return {
            "status": "empty",
            "paper_id": "",
            "reading_card": {},
            "source_scope": "none",
            "requires_fulltext": True,
            "warning": "当前运行没有可供阅读的候选论文",
        }
    if not paper_id or paper_id.startswith("candidate:"):
        return {
            "status": "unavailable",
            "paper_id": paper_id,
            "reading_card": {},
            "source_scope": "metadata_or_abstract",
            "requires_fulltext": True,
            "warning": "候选论文尚未导入本地文献库或没有本地 PDF；Agent 不会静默下载论文",
        }
    if payload.get("confirm") is not True:
        return {
            "status": "unavailable",
            "paper_id": paper_id,
            "reading_card": {},
            "source_scope": "consent_required",
            "requires_fulltext": True,
            "warning": "发送全文和页面图像给模型前需要明确确认；请以 consent_external=true 批准本次 Agent 运行",
        }
    with _services().DB_LOCK, _services().db_session() as db:
        exists = db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone()
        attachment_count = (
            int(
                db.execute(
                    "SELECT COUNT(*) FROM attachments WHERE paper_id=?", (paper_id,)
                ).fetchone()[0]
            )
            if exists
            else 0
        )
    if not exists:
        return {
            "status": "unavailable",
            "paper_id": paper_id,
            "reading_card": {},
            "source_scope": "metadata_or_abstract",
            "requires_fulltext": True,
            "warning": "论文不在本地文献库，无法进行可追溯全文分析",
        }
    if not attachment_count:
        return {
            "status": "unavailable",
            "paper_id": paper_id,
            "reading_card": {},
            "source_scope": "metadata_or_abstract",
            "requires_fulltext": True,
            "warning": "论文没有本地 PDF；请先下载或添加附件后再运行全文精读",
        }
    settings = _services().model_settings()
    if (
        not settings.get("enabled")
        or not settings.get("api_key_set")
        or not settings.get("model")
        or not (settings.get("api_url") or settings.get("base_url"))
    ):
        return {
            "status": "unavailable",
            "paper_id": paper_id,
            "reading_card": {},
            "source_scope": "local_fulltext_pending_model",
            "requires_fulltext": True,
            "warning": "全文阅读需要已启用的 OpenAI 兼容模型配置（API 地址、模型名和密钥）",
        }
    options = {
        "template": _services().clean_text(
            payload.get("template", "fulltext_reading_card"), 120
        ),
        "language": _services().clean_text(payload.get("language", "zh-CN"), 30),
        "apply": False,
        "merge_empty": True,
    }
    submitted = _services().submit_ai_job(paper_id, "reading_card", options)
    job = submitted.get("job") if isinstance(submitted, dict) else None
    job_id = _services().clean_text((job or {}).get("id", ""), 100)
    if not job_id:
        raise RuntimeError("全文阅读任务提交失败：没有返回任务 ID")
    try:
        wait_seconds = min(max(int(payload.get("wait_seconds", 840)), 30), 900)
    except (TypeError, ValueError):
        wait_seconds = 840
    deadline = _services().time.monotonic() + wait_seconds
    last_progress = None
    while _services().time.monotonic() < deadline:
        snapshot = _services().get_ai_job(job_id, include_result=True)
        if not snapshot:
            raise RuntimeError("全文阅读任务已丢失，无法恢复结果")
        status = snapshot.get("status")
        if status == "success":
            result = (
                snapshot.get("result")
                if isinstance(snapshot.get("result"), dict)
                else {}
            )
            card = (
                result.get("reading_card")
                if isinstance(result.get("reading_card"), dict)
                else {}
            )
            if not card:
                raise RuntimeError("全文阅读任务完成但没有返回结构化阅读卡")
            return {
                "status": "completed",
                "paper_id": paper_id,
                "reading_card": card,
                "analysis": result,
                "artifact_id": snapshot.get("artifact_id", ""),
                "job_id": job_id,
                "meta": snapshot.get("meta", {}),
                "source_scope": "fulltext_multimodal",
                "requires_fulltext": False,
                "deduplicated": bool(submitted.get("deduplicated")),
            }
        if status in {"failed", "cancelled", "interrupted"}:
            detail = _services().clean_text(
                snapshot.get("error") or snapshot.get("message") or status, 1000
            )
            raise RuntimeError(f"全文阅读任务{status}：{detail}")
        progress = (
            snapshot.get("progress_current"),
            snapshot.get("progress_total"),
            snapshot.get("stage"),
            snapshot.get("message"),
        )
        if payload.get("step_id") and progress != last_progress:
            last_progress = progress
            _services()._agent_update_step(
                payload.get("run_id", ""),
                payload["step_id"],
                progress_current=int(snapshot.get("progress_current") or 0),
                progress_total=int(snapshot.get("progress_total") or 0),
                message=_services().clean_text(
                    snapshot.get("message") or snapshot.get("stage") or "等待全文分析",
                    300,
                ),
            )
        _services().time.sleep(0.75)
    raise TimeoutError(
        f"全文阅读任务在 {wait_seconds} 秒内未完成；任务 {job_id} 仍可在 AI 任务列表中继续观察"
    )


def _paper_analysis_overview(
    papers: list[dict],
    claims: list[dict] | None = None,
    verification: dict | None = None,
    critique: dict | None = None,
) -> dict:
    """Build a bounded, source-grounded professional analysis for each paper.

    This is deliberately local and deterministic.  It does not ask a model to
    invent a paper-level narrative: every positive statement is either a
    verbatim page/abstract excerpt or an existing Claim, while missing fields
    become explicit questions and actions.  A later model pass may summarize
    this object, but it cannot upgrade an ``abstract_only`` result to a
    full-text conclusion.
    """
    claims = claims if isinstance(claims, list) else []
    verification = verification if isinstance(verification, dict) else {}
    critique = critique if isinstance(critique, dict) else {}
    verification_by_id = {
        str(item.get("claim_id")): item
        for item in (verification.get("results") or [])
        if isinstance(item, dict)
    }
    required_sections = (
        "Abstract",
        "Introduction",
        "Methods",
        "Experiments",
        "Results",
        "Discussion",
        "Limitations",
        "Conclusion",
    )
    reproducibility_terms = {
        "数据/数据集": ["dataset", "benchmark", "corpus", "data set", "数据集", "基准"],
        "代码/实现": ["code", "github", "repository", "implementation", "开源", "代码"],
        "训练设置": [
            "hyperparameter",
            "learning rate",
            "batch size",
            "epoch",
            "训练设置",
            "超参数",
        ],
        "基线与对照": ["baseline", "compared with", "ablation", "基线", "消融", "对照"],
        "资源与成本": [
            "gpu",
            "compute",
            "inference time",
            "memory",
            "计算资源",
            "推理时间",
        ],
        "统计与不确定性": [
            "confidence interval",
            "standard deviation",
            "significance",
            "置信区间",
            "标准差",
            "显著性",
        ],
    }
    field_terms = {
        "problem": [
            "objective",
            "research question",
            "we address",
            "motivat",
            "研究问题",
            "目标",
            "挑战",
        ],
        "method": [
            "method",
            "methodology",
            "approach",
            "architecture",
            "algorithm",
            "模型",
            "方法",
            "网络",
        ],
        "assumptions": ["assume", "assumption", "under the assumption", "假设", "前提"],
        "data_and_experiments": [
            "dataset",
            "benchmark",
            "experiment",
            "evaluation",
            "数据集",
            "实验",
            "评测",
        ],
        "results": [
            "result",
            "accuracy",
            "performance",
            "outperform",
            "improv",
            "结果",
            "性能",
            "提升",
        ],
        "limitations": [
            "limitation",
            "failure",
            "future work",
            "threat",
            "局限",
            "不足",
            "失败",
            "未来工作",
        ],
    }

    def _clean_quote(value: object, limit: int = 420) -> str:
        text = (
            _services().re.sub(r"\s+", " ", _services().clean_text(value, 5000)).strip()
        )
        return text[:limit]

    def _claim_text(claim: dict) -> str:
        return (
            _services()
            .re.sub(
                r"^(原文摘录|摘要摘录|比较矩阵[^：:]*摘录)\s*[：:]\s*",
                "",
                _services().clean_text(claim.get("claim_text", ""), 1000),
            )
            .strip()
        )

    def _claim_refs(items: list[dict], paper_id: str, limit: int = 3) -> list[dict]:
        refs = []
        for claim in items:
            if not isinstance(claim, dict):
                continue
            page = claim.get("page")
            if page in (None, "", 0):
                continue
            quote = _claim_text(claim)
            if not quote:
                continue
            refs.append(
                {
                    "paper_id": paper_id,
                    "page": int(page),
                    "section": _services().clean_text(
                        claim.get("section") or "PDF 页面文本", 80
                    ),
                    "quote": quote[:420],
                    "evidence_id": _services().clean_text(
                        claim.get("evidence_id", ""), 100
                    ),
                    "source_sha256": _services().clean_text(
                        claim.get("source_sha256", ""), 120
                    ),
                    "evidence_type": "claim_page",
                }
            )
            if len(refs) >= limit:
                break
        return refs

    overviews = []
    for raw in [item for item in (papers or []) if isinstance(item, dict)][:8]:
        paper = dict(raw)
        paper_id = _services().clean_text(
            paper.get("local_paper_id") or paper.get("id", ""), 200
        )
        aliases = {
            value
            for value in (
                paper_id,
                _services().clean_text(paper.get("id", ""), 200),
                _services().clean_text(paper.get("local_paper_id", ""), 200),
            )
            if value
        }
        paper_claims = [
            claim
            for claim in claims
            if isinstance(claim, dict)
            and aliases.intersection(
                {
                    _services().clean_text(claim.get("paper_id", ""), 200),
                    _services().clean_text(claim.get("local_paper_id", ""), 200),
                }
            )
        ]
        by_type = {}
        for claim in paper_claims:
            by_type.setdefault(
                _services().clean_text(claim.get("claim_type") or "other", 80), []
            ).append(claim)
        page_rows = []
        with _services().DB_LOCK, _services().db_session() as db:
            if paper_id and not paper_id.startswith("candidate:"):
                page_rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT page_number,text,text_sha256,quality FROM document_pages WHERE paper_id=? AND quality<>'empty' AND trim(text)<>'' ORDER BY page_number",
                        (paper_id,),
                    ).fetchall()
                ]
        page_sections = {}
        for row in page_rows:
            section = _services()._infer_page_section(row.get("text", ""))
            page_sections.setdefault(section, []).append(row)
        normalized_pages = [
            (
                row,
                _services()
                .re.sub(
                    r"\s+", " ", _services().clean_text(row.get("text", ""), 500000)
                )
                .strip(),
            )
            for row in page_rows
        ]

        def _page_refs(terms: list[str], limit: int = 3) -> list[dict]:
            refs = []
            lowered = [term.casefold() for term in terms]
            for row, text in normalized_pages:
                haystack = text.casefold()
                position = next(
                    (
                        haystack.find(term)
                        for term in lowered
                        if term and haystack.find(term) >= 0
                    ),
                    -1,
                )
                if position < 0:
                    continue
                start = max(0, position - 150)
                quote = text[start : min(len(text), position + 360)]
                refs.append(
                    {
                        "paper_id": paper_id,
                        "page": int(row.get("page_number") or 0),
                        "section": _services()._infer_page_section(row.get("text", "")),
                        "quote": _clean_quote(quote),
                        "source_sha256": _services().clean_text(
                            row.get("text_sha256", ""), 120
                        ),
                        "evidence_type": "page_text",
                    }
                )
                if len(refs) >= limit:
                    break
            return refs

        def _abstract_ref() -> list[dict]:
            abstract = _clean_quote(paper.get("abstract", ""), 620)
            if not abstract:
                return []
            return [
                {
                    "paper_id": paper_id
                    or _services().clean_text(paper.get("id", ""), 200),
                    "page": None,
                    "section": "abstract",
                    "quote": abstract,
                    "source_sha256": _services()
                    .hashlib.sha256(abstract.encode("utf-8"))
                    .hexdigest(),
                    "evidence_type": "abstract_text",
                }
            ]

        def _field(name: str, claim_types: tuple[str, ...], terms: list[str]) -> dict:
            selected = []
            for kind in claim_types:
                selected.extend(by_type.get(kind, []))
            refs = _claim_refs(selected, paper_id, 3) if page_rows else []
            if not refs:
                refs = _page_refs(terms, 3) if page_rows else _abstract_ref()
            text_values = [_claim_text(item) for item in selected if _claim_text(item)]
            if not text_values and refs:
                text_values = [ref["quote"] for ref in refs[:2]]
            if not text_values and name == "problem" and paper.get("abstract"):
                text_values = [_clean_quote(paper.get("abstract"), 620)]
            scope = (
                "fulltext_page"
                if any(ref.get("page") is not None for ref in refs)
                else ("abstract_only" if refs else "unverified")
            )
            return {
                "text": (
                    "；".join(dict.fromkeys(text_values))[:1200]
                    if text_values
                    else "未在已保存来源中定位"
                ),
                "evidence_refs": refs,
                "status": "verified" if scope == "fulltext_page" else scope,
            }

        analysis = {
            "problem": _field(
                "problem", ("research_question",), field_terms["problem"]
            ),
            "method": _field("method", ("method",), field_terms["method"]),
            "assumptions": _field(
                "assumptions", ("assumption",), field_terms["assumptions"]
            ),
            "data_and_experiments": _field(
                "data_and_experiments",
                ("dataset", "result"),
                field_terms["data_and_experiments"],
            ),
            "results": _field("results", ("result",), field_terms["results"]),
            "limitations": _field(
                "limitations", ("limitation",), field_terms["limitations"]
            ),
        }
        section_coverage = {
            section: {
                "pages": [
                    int(row.get("page_number") or 0)
                    for row in page_sections.get(section, [])
                ],
                "page_count": len(page_sections.get(section, [])),
            }
            for section in required_sections
        }
        detected_sections = [
            section
            for section, value in section_coverage.items()
            if value["page_count"]
        ]
        missing_sections = [
            section for section in required_sections if section not in detected_sections
        ]
        reproducibility = []
        for label, terms in reproducibility_terms.items():
            refs = _page_refs(terms, 2) if page_rows else []
            reproducibility.append(
                {
                    "item": label,
                    "status": "evidence_found" if refs else "not_located",
                    "evidence_refs": refs,
                    "next_step": (
                        "核对原文中的具体设置、版本或数值"
                        if refs
                        else "回到全文搜索该项，并在必要时查看附录/代码仓库"
                    ),
                }
            )
        visual_refs = (
            _page_refs(["figure", "table", "equation", "图", "表", "公式"], 6)
            if page_rows
            else []
        )
        risks = []
        for item in critique.get("critiques") or []:
            if isinstance(item, dict) and (
                not item.get("claim_id")
                or any(
                    str(claim.get("id")) == str(item.get("claim_id"))
                    for claim in paper_claims
                )
            ):
                risks.append(
                    {
                        "category": _services().clean_text(
                            item.get("category") or "review", 80
                        ),
                        "severity": _services().clean_text(
                            item.get("severity") or "medium", 20
                        ),
                        "text": _services().clean_text(item.get("critique", ""), 700),
                        "claim_id": _services().clean_text(
                            item.get("claim_id", ""), 100
                        ),
                    }
                )
        open_questions = []
        for section in missing_sections:
            open_questions.append(
                f"{section} 未在保存的页面文本中可靠定位，需要人工确认是否存在、是否提取失败或是否被合并到其他章节"
            )
        for item in reproducibility:
            if item["status"] == "not_located":
                open_questions.append(f"尚未定位{item['item']}证据")
        if not page_rows:
            open_questions.append(
                "当前只有摘要/元数据，不能判断公式、图表、实验设置、局限和复现条件"
            )
        next_actions = []
        if page_rows:
            if missing_sections:
                next_actions.append(
                    {
                        "priority": "high",
                        "action": "核对缺失章节或重新提取/ OCR 页面",
                        "why": "避免把未读取到的章节误判为论文没有该内容",
                        "sections": missing_sections[:4],
                    }
                )
            if visual_refs:
                next_actions.append(
                    {
                        "priority": "high",
                        "action": "逐页查看图表、公式和消融结果",
                        "why": "文本提取不能可靠表达视觉布局、公式和图例关系",
                        "pages": [ref["page"] for ref in visual_refs[:6]],
                    }
                )
            next_actions.append(
                {
                    "priority": "medium",
                    "action": "按复现清单核对数据、代码、训练设置和基线公平性",
                    "why": "形成可执行复现判断，而不是只复述摘要",
                }
            )
        else:
            next_actions.append(
                {
                    "priority": "high",
                    "action": "获取并提取 PDF 全文后再做页级精读",
                    "why": "当前证据范围仅为摘要，不能完成专业全文判断",
                }
            )
        evidence_count = sum(
            len(value.get("evidence_refs") or []) for value in analysis.values()
        ) + sum(len(item.get("evidence_refs") or []) for item in reproducibility)
        source_scope = (
            "fulltext_page"
            if page_rows
            else ("abstract_only" if paper.get("abstract") else "no_source")
        )
        overviews.append(
            {
                "paper_id": paper_id
                or _services().clean_text(paper.get("id", ""), 200),
                "title": _services().clean_text(paper.get("title", ""), 500),
                "year": paper.get("year"),
                "venue": _services().clean_text(paper.get("venue", ""), 200),
                "source_scope": source_scope,
                "analysis": analysis,
                "section_coverage": section_coverage,
                "evidence_coverage": {
                    "page_count": len(page_rows),
                    "detected_sections": detected_sections,
                    "missing_sections": missing_sections,
                    "evidence_count": evidence_count,
                    "coverage_percent": (
                        round(len(detected_sections) / len(required_sections) * 100)
                        if page_rows
                        else 0
                    ),
                },
                "reproducibility": reproducibility,
                "visual_inventory": visual_refs,
                "critical_risks": risks[:20],
                "open_questions": open_questions[:30],
                "next_actions": next_actions[:10],
                "uncertainties": (
                    ["全文页面文本已保存，但章节识别依赖页面标题，仍需人工确认章节边界"]
                    if page_rows
                    else ["没有本地页级全文证据，所有结论均限制在摘要/元数据范围"]
                )
                + (["批判项来自规则化检查，不能替代领域专家判断"] if risks else []),
            }
        )
    fulltext_count = sum(
        item.get("source_scope") == "fulltext_page" for item in overviews
    )
    abstract_only_count = sum(
        item.get("source_scope") == "abstract_only" for item in overviews
    )
    evidence_count = sum(
        int(item.get("evidence_coverage", {}).get("evidence_count") or 0)
        for item in overviews
    )
    return {
        "paper_overviews": overviews,
        "overview_count": len(overviews),
        "fulltext_count": fulltext_count,
        "abstract_only_count": abstract_only_count,
        "evidence_count": evidence_count,
        "status": "completed" if overviews else "unavailable",
        "warning": "分析总览只使用已保存的页面文本、摘要和已有 Claim；未定位内容会明确标记为缺口",
    }


def call_agent_tool(tool_name: str, payload: dict) -> dict:
    """Execute one bounded, auditable tool using existing local capabilities."""
    spec = _services().AGENT_TOOL_SPECS.get(tool_name)
    if not spec or not spec.get("available"):
        raise ValueError(f"Agent 工具不可用：{tool_name}")
    payload = payload if isinstance(payload, dict) else {}
    input_errors = _services()._agent_schema_errors(
        spec.get("input_schema") or {}, payload
    )
    if input_errors:
        raise ValueError("工具输入结构校验失败：" + "；".join(input_errors[:4]))
    if spec["class"] in {"Apply", "External"} and payload.get("confirm") is not True:
        raise PermissionError(
            f"工具 {tool_name} 属于 {spec['class']} 操作，需要显式确认"
        )
    # generate_research_brief always has a deterministic local-only path. Its
    # optional model synthesis is enabled only after run-level consent, so a
    # direct preview call remains safe and useful. Full-text drafts are
    # different: the caller must explicitly approve sending page text/images.
    if (
        tool_name == "draft_reading_card"
        and payload.get("paper")
        and payload.get("confirm") is not True
    ):
        paper = payload.get("paper") if isinstance(payload.get("paper"), dict) else {}
        local_id = _services().clean_text(
            paper.get("local_paper_id") or paper.get("id", ""), 200
        )
        has_fulltext = False
        if local_id and not local_id.startswith("candidate:"):
            with _services().DB_LOCK, _services().db_session() as db:
                has_fulltext = bool(
                    db.execute(
                        "SELECT 1 FROM attachments WHERE paper_id=? LIMIT 1",
                        (local_id,),
                    ).fetchone()
                )
        if (
            has_fulltext
            and _services().model_settings().get("enabled")
            and _services().model_api_key()
        ):
            raise PermissionError(
                "全文阅读卡会发送本地 PDF 的页面文本和图像，需要明确确认"
            )
    if tool_name == "build_search_plan":
        goal = payload.get("goal") or {}
        query = _services().clean_text(
            goal.get("core_question") or goal.get("objective", ""), 1000
        )
        sources = [
            source
            for source in (goal.get("sources") or ["arxiv", "openalex", "crossref"])
            if source in _services().SOURCE_SEARCH
        ]
        result = _services().search_query_plan(
            query,
            sources,
            {
                "expand_concepts": True,
                "expand_relations": True,
                "search_depth": "balanced",
            },
        )
        result["query"] = result.get("original_query", query)
        return result
    if tool_name == "search_sources":
        goal = payload.get("goal") or {}
        query_plan = (
            payload.get("query_plan")
            if isinstance(payload.get("query_plan"), dict)
            else {}
        )
        query = _services().clean_text(
            query_plan.get("original_query")
            or query_plan.get("query")
            or goal.get("core_question")
            or goal.get("objective", ""),
            1000,
        )
        planned_sources = [
            item.get("id")
            for item in (query_plan.get("sources") or [])
            if isinstance(item, dict) and item.get("id") in _services().SOURCE_SEARCH
        ]
        sources = planned_sources or [
            source
            for source in (goal.get("sources") or ["arxiv", "openalex", "crossref"])
            if source in _services().SOURCE_SEARCH
        ]
        filters = (
            query_plan.get("filters")
            if isinstance(query_plan.get("filters"), dict)
            else {
                "search_depth": "balanced",
                "expand_concepts": True,
                "expand_relations": True,
            }
        )
        filters = {**filters, "auto_add": False}
        result = _services().run_query(
            query,
            sources,
            min(max(int(payload.get("limit", 30)), 5), 50),
            options=filters,
            plan_override=query_plan or None,
        )
        papers, quality = _services().filter_agent_candidate_quality(
            result.get("papers", []), query
        )
        summary = dict(result.get("summary") or {})
        summary["agent_quality"] = quality
        summary["filtered_count"] = quality.get("filtered_count", 0)
        if quality.get("gaps"):
            gaps = list(summary.get("gaps") or [])
            summary["gaps"] = list(dict.fromkeys(gaps + quality["gaps"]))
        return {
            "run_id": result["id"],
            "status": result["status"],
            "candidate_mode": True,
            "summary": summary,
            "candidate_ids": [paper["id"] for paper in papers],
            "papers": [_services()._agent_reduced_paper(paper) for paper in papers],
        }
    if tool_name == "browse_web":
        raw_urls = payload.get("urls") if isinstance(payload.get("urls"), list) else []
        if payload.get("url"):
            raw_urls = [payload.get("url"), *raw_urls]
        try:
            max_chars = min(max(int(payload.get("max_chars", 12000)), 500), 20000)
        except (TypeError, ValueError):
            max_chars = 12000
        return _services().browse_public_web(raw_urls, max_chars)
    if tool_name == "fetch_web_page":
        try:
            max_chars = min(max(int(payload.get("max_chars", 12000)), 500), 20000)
        except (TypeError, ValueError):
            max_chars = 12000
        return _services().fetch_deepseek_page(
            _services().clean_text(payload.get("url"), 2400), max_chars
        )
    if tool_name == "search_local_library":
        return _services().search_library(
            _services().clean_text(payload.get("query", ""), 500),
            min(max(int(payload.get("limit", 30)), 1), 100),
            0,
            "",
            "",
            {},
        )
    if tool_name == "resolve_paper_identity":
        papers = (
            payload.get("papers") if isinstance(payload.get("papers"), list) else []
        )
        if papers:
            return _services()._agent_resolve_paper_identities(
                papers, limit=min(max(int(payload.get("limit", 100)), 1), 100)
            )
        identity = _services().clean_text(
            payload.get("doi") or payload.get("arxiv_id") or payload.get("title", ""),
            1000,
        )
        with _services().DB_LOCK, _services().db_session() as db:
            doi = _services().norm_doi(payload.get("doi") or identity)
            arxiv_id = _services().norm_arxiv(payload.get("arxiv_id") or identity)
            title = _services().title_key(payload.get("title") or identity)
            row = db.execute(
                "SELECT * FROM papers WHERE (doi<>'' AND doi=?) OR (arxiv_id<>'' AND arxiv_id=?) OR (title_key<>'' AND title_key=?) LIMIT 1",
                (doi, arxiv_id, title),
            ).fetchone()
        return {
            "resolved": bool(row),
            "paper": _services().paper_dict(row) if row else None,
            "query": identity,
            "identity_status": (
                "doi"
                if row and doi and _services().norm_doi(row["doi"]) == doi
                else (
                    "arxiv"
                    if row
                    and arxiv_id
                    and _services().norm_arxiv(row["arxiv_id"]) == arxiv_id
                    else ("title" if row else "unresolved")
                )
            ),
        }
    if tool_name == "merge_paper_versions":
        papers = [
            item for item in (payload.get("papers") or []) if isinstance(item, dict)
        ]
        identity_output = (
            payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
        )
        # The identity step can enrich a candidate with its local paper ID and
        # stable key.  Merge those annotations back without replacing newer
        # provider metadata from the search snapshot.
        resolved_items = (
            identity_output.get("papers")
            if isinstance(identity_output.get("papers"), list)
            else []
        )
        if resolved_items:
            papers = _services()._unique_agent_papers(papers, resolved_items, limit=100)
        groups = {}
        for paper in papers[:100]:
            key = _services().candidate_identity(paper)
            doi = _services().norm_doi(paper.get("doi", ""))
            arxiv_id = _services().norm_arxiv(paper.get("arxiv_id", ""))
            canonical = (
                ("doi:" + doi)
                if doi
                else (
                    ("arxiv:" + arxiv_id)
                    if arxiv_id
                    else "title:" + _services().title_key(paper.get("title", ""))
                )
            )
            groups.setdefault(canonical, []).append(
                {
                    **_services()._agent_reduced_paper(paper),
                    "paper_key": key,
                    "version_key": canonical,
                }
            )
        # DOI and arXiv identifiers can legitimately refer to different
        # editions of one work.  A conservative second pass joins only very
        # high title matches with compatible years and overlapping authors.
        canonical_keys = list(groups)
        for index, left_key in enumerate(canonical_keys):
            if left_key not in groups:
                continue
            left = groups[left_key][0]
            for right_key in canonical_keys[index + 1 :]:
                if right_key not in groups:
                    continue
                right = groups[right_key][0]
                if (
                    not left.get("title")
                    or not right.get("title")
                    or _services().title_similarity(left["title"], right["title"])
                    < 0.95
                ):
                    continue
                left_year, right_year = left.get("year"), right.get("year")
                if (
                    left_year
                    and right_year
                    and abs(int(left_year) - int(right_year)) > 2
                ):
                    continue
                left_authors = set(
                    _services().relevance_tokens(left.get("authors", ""))
                )
                right_authors = set(
                    _services().relevance_tokens(right.get("authors", ""))
                )
                if (
                    left_authors
                    and right_authors
                    and not (left_authors & right_authors)
                ):
                    continue
                version_key = (
                    "work:"
                    + _services()
                    .hashlib.sha1(
                        (
                            _services().title_key(left["title"])
                            + "|"
                            + _services().title_key(right["title"])
                        ).encode("utf-8")
                    )
                    .hexdigest()[:20]
                )
                merged_versions = groups.pop(left_key) + groups.pop(right_key)
                for version in merged_versions:
                    version["version_key"] = version_key
                groups[version_key] = merged_versions
                left = groups[version_key][0]
        now = _services().now_iso()
        persisted = []
        with _services().DB_LOCK, _services().db_session() as db:
            goal = payload.get("goal") or {}
            goal_id = _services().clean_text(goal.get("id", ""), 80)
            run_id = _services().clean_text(payload.get("run_id", ""), 80)
            for canonical, versions in groups.items():
                relation = (
                    "same_work"
                    if len(versions) == 1
                    else "version_or_provider_duplicate"
                )
                for version in versions:
                    paper_key = version["paper_key"]
                    db.execute(
                        """INSERT INTO paper_version_links(canonical_key,paper_key,relation,confidence,metadata_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(canonical_key,paper_key) DO UPDATE SET relation=excluded.relation,confidence=excluded.confidence,metadata_json=excluded.metadata_json,last_seen_at=excluded.last_seen_at""",
                        (
                            canonical,
                            paper_key,
                            relation,
                            1.0 if canonical.startswith(("doi:", "arxiv:")) else 0.94,
                            _services().json.dumps(
                                {
                                    "title": version.get("title", ""),
                                    "year": version.get("year"),
                                },
                                ensure_ascii=False,
                            ),
                            now,
                            now,
                        ),
                    )
                persisted.append(
                    {
                        "canonical_key": canonical,
                        "relation": relation,
                        "versions": versions,
                    }
                )
            history = (
                _services()._record_goal_paper_history(db, goal_id, run_id, papers)
                if goal_id and run_id
                else {"new": [], "changed": [], "repeated": []}
            )
        return {
            "groups": persisted,
            "group_count": len(persisted),
            "duplicate_count": max(0, len(papers) - len(persisted)),
            "history": history,
        }
    if tool_name == "rank_candidates":
        papers = (
            payload.get("papers") if isinstance(payload.get("papers"), list) else []
        )
        goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else {}
        quality = None
        quality_query = _services().clean_text(
            goal.get("core_question") or goal.get("objective", ""), 1000
        )
        if quality_query:
            papers, quality = _services().filter_agent_candidate_quality(
                papers, quality_query
            )
        local_context = (
            payload.get("local_context")
            if isinstance(payload.get("local_context"), dict)
            else {}
        )
        local_rows = (
            local_context.get("results")
            if isinstance(local_context.get("results"), list)
            else []
        )
        ranked = sorted(
            papers,
            key=lambda item: (
                float(item.get("discovery_score") or item.get("relevance_score") or 0),
                int(item.get("year") or 0),
            ),
            reverse=True,
        )
        limit = min(max(int(payload.get("limit", 20)), 1), 50)
        candidates = []
        with _services().DB_LOCK, _services().db_session() as db:
            for item in ranked[:limit]:
                match = None
                if item.get("doi"):
                    match = db.execute(
                        "SELECT id FROM papers WHERE doi=? LIMIT 1",
                        (_services().norm_doi(item["doi"]),),
                    ).fetchone()
                if not match and item.get("arxiv_id"):
                    match = db.execute(
                        "SELECT id FROM papers WHERE arxiv_id=? LIMIT 1",
                        (_services().norm_arxiv(item["arxiv_id"]),),
                    ).fetchone()
                if not match:
                    match = db.execute(
                        "SELECT id FROM papers WHERE title_key=? AND (year=? OR ? IS NULL) LIMIT 1",
                        (
                            _services().title_key(item.get("title", "")),
                            item.get("year"),
                            item.get("year"),
                        ),
                    ).fetchone()
                reduced = _services()._agent_reduced_paper(item)
                reduced["local_paper_id"] = match["id"] if match else ""
                reduced["has_local_pdf"] = bool(
                    match
                    and db.execute(
                        "SELECT 1 FROM attachments WHERE paper_id=? LIMIT 1",
                        (match["id"],),
                    ).fetchone()
                )
                reduced["local_document_pages"] = (
                    int(
                        db.execute(
                            "SELECT COUNT(*) FROM document_pages WHERE paper_id=?",
                            (match["id"],),
                        ).fetchone()[0]
                    )
                    if match
                    else 0
                )
                related = []
                for context in local_rows[:30]:
                    if (
                        not isinstance(context, dict)
                        or not context.get("id")
                        or context.get("id") == item.get("id")
                    ):
                        continue
                    overlap = (
                        _services()
                        .text_overlap(
                            _services().paper_text(item),
                            _services().paper_text(context),
                        )
                        .get("score", 0)
                    )
                    if overlap >= 22:
                        related.append(
                            {
                                "paper_id": context.get("id"),
                                "title": context.get("title", ""),
                                "overlap": overlap,
                            }
                        )
                reduced["related_local_papers"] = sorted(
                    related, key=lambda value: value["overlap"], reverse=True
                )[:3]
                candidates.append(reduced)
        focus = _services()._agent_focus_papers(
            candidates, limit=min(3, len(candidates))
        )
        focus_by_id = {item.get("id"): item for item in focus}
        for item in candidates:
            focused = focus_by_id.get(item.get("id"))
            if focused:
                item["focus_rank"] = focused.get("focus_rank")
                item["focus_reason"] = focused.get("focus_reason")
        output = {
            "total": len(ranked),
            "high_relevance": sum(
                item.get("relevance_label") == "高相关" for item in ranked
            ),
            "candidates": candidates,
            "excluded": max(0, len(ranked) - limit),
            "local_context_count": int(local_context.get("total") or len(local_rows)),
        }
        if quality is not None:
            output["quality"] = quality
            output["filtered_count"] = quality.get("filtered_count", 0)
        return output
    if tool_name == "evaluate_coverage":
        goal = payload.get("goal") or {}
        triage = payload.get("triage") or {}
        candidates = (
            triage.get("candidates")
            if isinstance(triage.get("candidates"), list)
            else []
        )
        labels = {
            str(item.get("topic") or "").strip()
            for item in candidates
            if item.get("topic")
        }
        abstracts = sum(bool(item.get("abstract")) for item in candidates)
        fulltext = sum(
            bool(item.get("local_document_pages") or item.get("has_local_pdf"))
            for item in candidates
        )
        gaps = []
        focus = goal.get("focus") or []
        if not candidates:
            gaps.append("没有找到候选论文")
        if len(candidates) < 5:
            gaps.append("候选数量不足以覆盖研究问题")
        if not abstracts:
            gaps.append("候选论文缺少摘要")
        if focus and not labels:
            gaps.append("研究目标关注维度尚未形成可分组主题")
        if goal.get("workflow") == "evidence_review" and not fulltext:
            gaps.append("尚无本地全文页级证据")
        return {
            "coverage": {
                "candidate_count": len(candidates),
                "abstract_count": abstracts,
                "fulltext_count": fulltext,
                "topic_count": len(labels),
                "focus_count": len(focus),
            },
            "gaps": gaps,
            "needs_expansion": bool(gaps and len(candidates) < 30),
            "stop_reason": (
                "覆盖达到当前预算" if not gaps else "存在可通过一次补搜改善的缺口"
            ),
        }
    if tool_name == "expand_search":
        goal = payload.get("goal") or {}
        coverage = payload.get("coverage") or (payload.get("previous") or {}).get(
            "evaluate_coverage", {}
        )
        if not coverage.get("needs_expansion"):
            return {
                "expanded": False,
                "reason": "当前覆盖已足够，无需补搜",
                "papers": [],
                "candidate_ids": [],
            }
        query = _services().clean_text(
            goal.get("core_question") or goal.get("objective", ""), 1000
        )
        sources = [
            source
            for source in (goal.get("sources") or _services().SOURCE_SEARCH.keys())
            if source in _services().SOURCE_SEARCH
        ]
        result = _services().run_query(
            query + " limitations failure analysis",
            sources,
            min(max(int(payload.get("limit", 15)), 5), 30),
            options={
                "search_depth": "focused",
                "expand_concepts": False,
                "expand_relations": False,
                "auto_add": False,
            },
        )
        papers = [
            _services()._agent_reduced_paper(item) for item in result.get("papers", [])
        ]
        with _services().DB_LOCK, _services().db_session() as db:
            history = (
                _services()._record_goal_paper_history(
                    db,
                    _services().clean_text(goal.get("id", ""), 80),
                    _services().clean_text(payload.get("run_id", ""), 80),
                    papers,
                )
                if goal.get("id")
                else {"new": [], "changed": [], "repeated": []}
            )
        return {
            "expanded": True,
            "run_id": result.get("id", ""),
            "papers": papers,
            "candidate_ids": [paper.get("id") for paper in papers],
            "history": history,
            "summary": result.get("summary", {}),
        }
    if tool_name == "prepare_fulltext":
        papers = (
            payload.get("papers") if isinstance(payload.get("papers"), list) else []
        )
        prepared, ready_count, downloadable_count, missing_count = [], 0, 0, 0
        seen_keys = set()
        with _services().DB_LOCK, _services().db_session() as db:
            for item in papers[:8]:
                if not isinstance(item, dict):
                    continue
                paper_id = _services().clean_text(
                    item.get("local_paper_id") or item.get("id", ""), 200
                )
                row = (
                    db.execute(
                        "SELECT id,title,pdf_url,url,arxiv_id,doi FROM papers WHERE id=?",
                        (paper_id,),
                    ).fetchone()
                    if paper_id and not paper_id.startswith("candidate:")
                    else None
                )
                # Expanded searches can return a new candidate ID for the same work.
                # Collapse those aliases before counting download/readiness gaps.
                identity = _services().candidate_identity(
                    {
                        **item,
                        **(dict(row) if row else {}),
                    }
                )
                identity = identity or f"id:{paper_id}"
                if identity in seen_keys:
                    continue
                seen_keys.add(identity)
                attachment_count = (
                    int(
                        db.execute(
                            "SELECT COUNT(*) FROM attachments WHERE paper_id=?",
                            (paper_id,),
                        ).fetchone()[0]
                    )
                    if row
                    else 0
                )
                page_count = (
                    int(
                        db.execute(
                            "SELECT COUNT(*) FROM document_pages WHERE paper_id=?",
                            (paper_id,),
                        ).fetchone()[0]
                    )
                    if row
                    else 0
                )
                urls = (
                    _services().paper_pdf_candidates(row)
                    if row
                    else [_services().clean_text(item.get("pdf_url", ""), 3000)]
                )
                urls = [url for url in urls if url]
                if attachment_count:
                    state = "ready"
                    ready_count += 1
                elif urls:
                    state = "downloadable"
                    downloadable_count += 1
                else:
                    state = "missing"
                    missing_count += 1
                prepared.append(
                    {
                        "paper_id": paper_id or item.get("id", ""),
                        "title": item.get("title") or (row["title"] if row else ""),
                        "state": state,
                        "attachment_count": attachment_count,
                        "page_count": page_count,
                        "download_urls": urls[:4],
                        "requires_user_action": state == "downloadable",
                        "reason": (
                            "已有本地 PDF"
                            if state == "ready"
                            else (
                                "存在公开地址，需用户确认下载"
                                if state == "downloadable"
                                else "没有可用的公开 PDF 地址"
                            )
                        ),
                    }
                )
        return {
            "papers": prepared,
            "ready_count": ready_count,
            "downloadable_count": downloadable_count,
            "missing_count": missing_count,
            "status": (
                "ready"
                if not downloadable_count and not missing_count
                else (
                    "needs_user_download"
                    if downloadable_count
                    else "no_public_fulltext"
                )
            ),
        }
    if tool_name == "download_fulltext":
        if payload.get("confirm") is not True:
            raise PermissionError("下载公开 PDF 会访问外部数据源，需要显式确认")
        requested = (
            payload.get("papers") if isinstance(payload.get("papers"), list) else []
        )
        selected_ids = list(
            dict.fromkeys(
                _services().clean_text(value, 200)
                for value in (payload.get("paper_ids") or [])
                if _services().clean_text(value, 200)
            )
        )
        if not selected_ids:
            selected_ids = list(
                dict.fromkeys(
                    _services().clean_text(item.get("local_paper_id"), 200)
                    for item in requested
                    if isinstance(item, dict)
                    and _services().clean_text(item.get("local_paper_id"), 200)
                )
            )
        if not selected_ids or len(selected_ids) > 8:
            raise ValueError("请选择 1 到 8 篇已经导入文献库的论文下载 PDF")
        prepared = {
            _services().clean_text(item.get("local_paper_id"), 200): item
            for item in requested
            if isinstance(item, dict)
        }
        outcomes = {}

        def download_one(paper_id):
            item = prepared.get(paper_id, {})
            url_candidates = (
                item.get("download_urls")
                if isinstance(item.get("download_urls"), list)
                else []
            )
            url = url_candidates[0] if url_candidates else ""
            try:
                return {
                    "ok": True,
                    "paper_id": paper_id,
                    "result": _services().download_public_pdf(paper_id, url),
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "paper_id": paper_id,
                    "error": _services().clean_text(str(exc), 500),
                }

        worker_count = min(
            _services().PDF_DOWNLOAD_CONCURRENCY, max(1, len(selected_ids))
        )
        with _services().ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="agent-pdf-download"
        ) as pool:
            futures = {
                pool.submit(download_one, paper_id): paper_id
                for paper_id in selected_ids
            }
            completed_count = 0
            for future in _services().as_completed(futures):
                outcome = future.result()
                outcomes[outcome["paper_id"]] = outcome
                completed_count += 1
                if payload.get("step_id"):
                    _services()._agent_update_step(
                        payload.get("run_id", ""),
                        payload["step_id"],
                        progress_current=completed_count,
                        progress_total=len(selected_ids),
                        message=f"已下载 {completed_count}/{len(selected_ids)} 篇 PDF",
                    )

        # Keep the API deterministic even though completion order depends on
        # network latency.  The caller can render results beside its selection.
        results = [
            {"paper_id": paper_id, **outcomes[paper_id]["result"]}
            for paper_id in selected_ids
            if outcomes.get(paper_id, {}).get("ok")
        ]
        failures = [
            {"paper_id": paper_id, "error": outcomes[paper_id]["error"]}
            for paper_id in selected_ids
            if paper_id in outcomes and not outcomes[paper_id]["ok"]
        ]
        return {
            "status": (
                "completed"
                if results and not failures
                else ("partial" if results else "failed")
            ),
            "downloaded": results,
            "failed": failures,
            "downloaded_count": len(results),
            "failed_count": len(failures),
        }
    if tool_name == "extract_pdf_pages":
        paper_ids = [
            _services().clean_text(item, 200)
            for item in (payload.get("paper_ids") or [])
            if _services().clean_text(item, 200)
        ]
        if not paper_ids and payload.get("paper_id"):
            paper_ids = [_services().clean_text(payload.get("paper_id"), 200)]
        paper_ids = list(dict.fromkeys(paper_ids))[:8]
        results, skipped = [], []

        def extract_one(paper_id):
            cached = _services().extraction_summary(paper_id)
            if cached.get("attachment_count") and cached.get(
                "attachment_count"
            ) == cached.get("extracted_attachment_count"):
                return {
                    "paper_id": paper_id,
                    "page_count": cached.get("page_count", 0),
                    "characters": cached.get("characters", 0),
                    "cached": True,
                }
            return _services().extract_paper_documents(paper_id)

        # PDF parsing is independent per paper.  A small bounded pool avoids
        # making a long Agent run wait serially while keeping SQLite writes
        # protected by DB_LOCK inside the extraction helpers.  Results are
        # sorted back into input order so artifacts remain deterministic.
        completed = {}
        worker_count = min(3, max(1, len(paper_ids)))
        with _services().ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="agent-pdf-extract"
        ) as pool:
            futures = {
                pool.submit(extract_one, paper_id): paper_id for paper_id in paper_ids
            }
            for future in _services().as_completed(futures):
                paper_id = futures[future]
                try:
                    completed[paper_id] = future.result()
                except ValueError as exc:
                    skipped.append({"paper_id": paper_id, "reason": str(exc)})
                except Exception as exc:
                    skipped.append(
                        {
                            "paper_id": paper_id,
                            "reason": f"PDF 解析失败：{_services().clean_text(str(exc), 300)}",
                        }
                    )
                if payload.get("step_id"):
                    _services()._agent_update_step(
                        payload.get("run_id", ""),
                        payload["step_id"],
                        progress_current=len(completed) + len(skipped),
                        progress_total=len(paper_ids),
                        message=f"已处理 {len(completed) + len(skipped)}/{len(paper_ids)} 篇本地 PDF",
                    )
        results = [
            completed[paper_id] for paper_id in paper_ids if paper_id in completed
        ]
        return {
            "papers_processed": len(results),
            "skipped": skipped,
            "results": results,
            "page_count": sum(int(item.get("page_count") or 0) for item in results),
            "characters": sum(int(item.get("characters") or 0) for item in results),
        }
    if tool_name == "ocr_pdf_pages":
        paper_ids = [
            _services().clean_text(item, 200)
            for item in (payload.get("paper_ids") or [])
            if _services().clean_text(item, 200)
        ]
        paper_ids = list(dict.fromkeys(paper_ids))[:8]
        max_pages = min(max(int(payload.get("max_pages", 24)), 1), 40)
        attempted = updated = skipped = 0
        pages_seen = []
        unavailable_reason = ""
        try:
            import fitz
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            unavailable_reason = f"OCR 依赖不可用：{exc.name or str(exc)}"
            return {
                "status": "unavailable",
                "attempted": 0,
                "updated": 0,
                "skipped": 0,
                "pages": [],
                "reason": unavailable_reason,
                "paper_count": len(paper_ids),
            }
        # Build the page worklist first.  Rendering and OCR are CPU/external
        # work, so they run in a bounded pool; SQLite updates stay in the main
        # thread to avoid write contention and to make progress durable.
        page_rows = []
        for paper_id in paper_ids:
            with _services().DB_LOCK, _services().db_session() as db:
                page_rows.extend(
                    dict(row)
                    for row in db.execute(
                        "SELECT d.attachment_id,d.paper_id,d.page_number,d.text,d.char_count,d.quality,a.stored_name "
                        "FROM document_pages d JOIN attachments a ON a.id=d.attachment_id "
                        "WHERE d.paper_id=? AND d.quality IN ('low','empty') ORDER BY d.page_number LIMIT ?",
                        (paper_id, max_pages),
                    )
                )

        language = _services().clean_text(payload.get("language", "eng"), 40) or "eng"

        def ocr_one(row):
            paper_id = row["paper_id"]
            page_number = int(row["page_number"])
            target = (
                _services().active_files_dir()
                / _services().Path(row["stored_name"]).name
            )
            base = {
                "paper_id": paper_id,
                "page": page_number,
                "attachment_id": row["attachment_id"],
            }
            if not target.is_file():
                return {**base, "status": "missing_file"}
            try:
                with fitz.open(str(target)) as document:
                    page_index = page_number - 1
                    if page_index < 0 or page_index >= len(document):
                        raise ValueError("页码超出 PDF 范围")
                    pixmap = document[page_index].get_pixmap(
                        matrix=fitz.Matrix(2, 2), alpha=False
                    )
                    image = Image.frombytes(
                        "RGB", [pixmap.width, pixmap.height], pixmap.samples
                    )
                    text = _services().clean_text(
                        pytesseract.image_to_string(image, lang=language), 500000
                    )
                chars = len(text.strip())
                if chars <= int(row.get("char_count") or 0):
                    return {**base, "status": "no_improvement", "characters": chars}
                digest = _services().hashlib.sha256(text.encode("utf-8")).hexdigest()
                return {
                    **base,
                    "status": "updated",
                    "text": text,
                    "characters": chars,
                    "quality": "good" if chars >= 160 else "low",
                    "text_sha256": digest,
                }
            except Exception as exc:
                return {
                    **base,
                    "status": "failed",
                    "reason": _services().clean_text(str(exc), 300),
                }

        outcomes = []
        worker_count = (
            min(_services().OCR_CONCURRENCY, max(1, len(page_rows))) if page_rows else 1
        )
        with _services().ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="agent-ocr"
        ) as pool:
            futures = [pool.submit(ocr_one, row) for row in page_rows]
            for completed_count, future in enumerate(
                _services().as_completed(futures), 1
            ):
                try:
                    outcome = future.result()
                except Exception as exc:
                    # ocr_one is defensive, but retain per-page isolation if a
                    # dependency raises outside its normal exception path.
                    outcome = {
                        "paper_id": "",
                        "page": 0,
                        "status": "failed",
                        "reason": _services().clean_text(str(exc), 300),
                    }
                outcomes.append(outcome)
                if payload.get("step_id"):
                    _services()._agent_update_step(
                        payload.get("run_id", ""),
                        payload["step_id"],
                        progress_current=completed_count,
                        progress_total=len(page_rows),
                        message=f"已完成 OCR {completed_count}/{len(page_rows)} 页",
                    )

        # Completion order is intentionally normalized for stable API output.
        order = {
            (
                str(row["paper_id"]),
                int(row["page_number"]),
                str(row["attachment_id"]),
            ): index
            for index, row in enumerate(page_rows)
        }
        outcomes.sort(
            key=lambda item: order.get(
                (
                    str(item.get("paper_id", "")),
                    int(item.get("page", 0)),
                    str(item.get("attachment_id", "")),
                ),
                len(order),
            )
        )
        for outcome in outcomes:
            attempted += 1
            status = outcome.get("status")
            if status == "updated":
                try:
                    with _services().DB_LOCK, _services().db_session() as db:
                        db.execute(
                            "UPDATE document_pages SET text=?,char_count=?,quality=?,extractor=?,text_sha256=?,extracted_at=? WHERE attachment_id=? AND page_number=?",
                            (
                                outcome["text"],
                                outcome["characters"],
                                outcome["quality"],
                                "pytesseract",
                                outcome["text_sha256"],
                                _services().now_iso(),
                                outcome["attachment_id"],
                                outcome["page"],
                            ),
                        )
                    updated += 1
                except Exception as exc:
                    status = "failed"
                    outcome["reason"] = _services().clean_text(str(exc), 300)
            if status != "updated":
                skipped += 1
            page_result = {
                "paper_id": outcome.get("paper_id", ""),
                "page": int(outcome.get("page", 0)),
                "status": status,
            }
            for key in ("characters", "text_sha256", "reason"):
                if key in outcome:
                    page_result[key] = outcome[key]
            pages_seen.append(page_result)
        status = (
            "updated"
            if updated
            else ("not_needed" if attempted == 0 else "no_improvement")
        )
        return {
            "status": status,
            "attempted": attempted,
            "updated": updated,
            "skipped": skipped,
            "pages": pages_seen,
            "paper_count": len(paper_ids),
            "reason": unavailable_reason,
        }
    if tool_name == "render_pdf_page":
        paper_id = _services().clean_text(payload.get("paper_id", ""), 200)
        page = max(1, int(payload.get("page", 1)))
        with _services().DB_LOCK, _services().db_session() as db:
            attachment = db.execute(
                "SELECT id,stored_name,sha256 FROM attachments WHERE paper_id=? ORDER BY added_at LIMIT 1",
                (paper_id,),
            ).fetchone()
        if not attachment:
            raise ValueError("论文没有本地 PDF 附件")
        rendered = _services().render_pdf_page_visual(dict(attachment), page)
        return {
            "paper_id": paper_id,
            "page": page,
            "attachment_id": attachment["id"],
            "url": "/visual-pages/" + _services().Path(rendered["path"]).name,
            "sha256": rendered.get("sha256", ""),
        }
    if tool_name == "search_document":
        return {
            "paper_id": payload.get("paper_id", ""),
            "query": payload.get("query", ""),
            "pages": _services().search_document_pages(
                _services().clean_text(payload.get("paper_id", ""), 200),
                _services().clean_text(payload.get("query", ""), 300),
                min(max(int(payload.get("limit", 20)), 1), 50),
            ),
        }
    if tool_name == "expand_citations":
        # Agent expansion is a read-only candidate lane.  The interactive
        # paper-details endpoint still calls expand_openalex_relations() with
        # its default persist=True, while this path never imports every hit.
        requested = (
            payload.get("papers") if isinstance(payload.get("papers"), list) else []
        )
        seed_items, seen_seeds = [], set()

        def add_seed(item: dict, local_id: str, identifier: str) -> None:
            """Add one citation seed using a work identity, not a transient ID."""
            if not identifier:
                return
            identity = (
                _services().candidate_identity({**item, "local_paper_id": local_id})
                or identifier
            )
            if identity in seen_seeds:
                return
            seed_items.append((identifier, dict(item), local_id))
            seen_seeds.add(identity)

        for item in requested:
            if not isinstance(item, dict):
                continue
            local_id = _services().clean_text(
                item.get("local_paper_id") or item.get("paper_id"), 200
            )
            # Candidate IDs are intentionally ephemeral snapshots, not rows in
            # `papers`; use their DOI/OpenAlex identity as the remote seed.
            if local_id.startswith("candidate:"):
                local_id = ""
            identifier = local_id or _services().clean_text(
                item.get("openalex_id")
                or item.get("external_id")
                or item.get("doi")
                or "",
                300,
            )
            add_seed(item, local_id, identifier)
        single_id = _services().clean_text(payload.get("paper_id", ""), 200)
        if single_id and not single_id.startswith("candidate:"):
            add_seed({"local_paper_id": single_id}, single_id, single_id)
        goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else {}
        goal_id = _services().clean_text(goal.get("id", ""), 80)
        if goal_id:
            # A follow-up run must retain the user's previously imported seeds
            # even when the current search is candidate-only.  This keeps the
            # citation graph connected across years without importing anything.
            with _services().DB_LOCK, _services().db_session() as db:
                history_rows = db.execute(
                    "SELECT local_paper_id,title,doi,arxiv_id,metadata_json FROM research_goal_papers WHERE goal_id=? ORDER BY last_seen_at DESC LIMIT 20",
                    (goal_id,),
                ).fetchall()
            for row in history_rows:
                local_id = _services().clean_text(row["local_paper_id"], 200)
                if local_id.startswith("candidate:"):
                    local_id = ""
                try:
                    metadata = _services().json.loads(row["metadata_json"] or "{}")
                except (TypeError, ValueError, _services().json.JSONDecodeError):
                    metadata = {}
                item = {
                    "local_paper_id": local_id,
                    "title": row["title"],
                    "doi": row["doi"],
                    "arxiv_id": row["arxiv_id"],
                    "openalex_id": metadata.get("openalex_id", ""),
                    "external_id": metadata.get("external_id", ""),
                }
                identifier = local_id or _services().clean_text(
                    item.get("openalex_id")
                    or item.get("external_id")
                    or item.get("doi")
                    or "",
                    300,
                )
                add_seed(item, local_id, identifier)
        seed_items = seed_items[:4]
        if not seed_items:
            return {
                "status": "skipped",
                "expanded": False,
                "reason": "没有已导入且具有 DOI/OpenAlex 标识的种子论文",
                "papers": [],
                "candidate_ids": [],
                "seed_count": 0,
            }
        limit = min(max(int(payload.get("limit", 24)), 1), 60)
        candidates, errors = [], []
        run_id = _services().clean_text(payload.get("run_id", ""), 80)
        step_id = _services().clean_text(payload.get("step_id", ""), 80)
        if run_id and step_id:
            _services()._agent_update_step(
                run_id,
                step_id,
                progress_current=0,
                progress_total=len(seed_items),
                message=f"并行扩展 {len(seed_items)} 个引用种子；每个种子最多 3 次 OpenAlex 请求",
            )

        def expand_seed(seed):
            seed_id, paper, local_id = seed
            try:
                if local_id:
                    # Evidence-review runs should spend their latency budget
                    # on the local PDF when one is already available. Citation
                    # expansion is useful for discovery, but it is optional
                    # once page-level evidence can be checked immediately.
                    with _services().DB_LOCK, _services().db_session() as db:
                        has_local_fulltext = bool(
                            db.execute(
                                "SELECT 1 FROM attachments WHERE paper_id=? LIMIT 1",
                                (local_id,),
                            ).fetchone()
                        )
                    if has_local_fulltext:
                        return seed_id, [], ""
                    # Agent citation discovery is a bounded recall lane.  The
                    # optional forward-citation endpoint can hold an entire
                    # evidence review behind a slow provider, so keep the
                    # primary work plus references/related works and let the
                    # normal interactive expansion provide the full graph.
                    try:
                        expanded = _services().expand_openalex_relations(
                            local_id,
                            limit=max(1, limit // len(seed_items)),
                            persist=False,
                            request_timeout=4,
                            include_cited_by=False,
                        )
                    except TypeError:
                        # Preserve compatibility with test doubles and older
                        # connector adapters that expose the legacy signature.
                        expanded = _services().expand_openalex_relations(
                            local_id, max(1, limit // len(seed_items)), persist=False
                        )
                else:
                    work_url, _ = _services()._openalex_work_reference(paper)
                    try:
                        expanded = _services()._expand_openalex_relation_work(
                            work_url,
                            limit=max(1, limit // len(seed_items)),
                            persist=False,
                            request_timeout=4,
                            include_cited_by=False,
                        )
                    except TypeError:
                        expanded = _services()._expand_openalex_relation_work(
                            work_url, max(1, limit // len(seed_items)), persist=False
                        )
                return seed_id, expanded.get("candidates", []), ""
            except Exception as exc:
                return seed_id, [], _services().clean_text(str(exc), 300)

        completed_seeds = {}
        with _services().ThreadPoolExecutor(
            max_workers=min(3, len(seed_items)), thread_name_prefix="agent-citation"
        ) as pool:
            futures = [pool.submit(expand_seed, seed) for seed in seed_items]
            for future in _services().as_completed(futures):
                seed_id, seed_candidates, error = future.result()
                completed_seeds[seed_id] = (seed_candidates, error)
                if error:
                    errors.append({"seed_paper_id": seed_id, "error": error})
                if run_id and step_id:
                    _services()._agent_update_step(
                        run_id,
                        step_id,
                        progress_current=len(completed_seeds),
                        progress_total=len(seed_items),
                        message=f"引用扩展完成 {len(completed_seeds)}/{len(seed_items)} 个种子",
                    )
        for seed_id, _, _ in seed_items:
            seed_candidates, _ = completed_seeds.get(seed_id, ([], ""))
            for item in seed_candidates:
                record = _services()._agent_reduced_paper(item)
                record["relation"] = item.get("relation", "")
                record["seed_paper_id"] = seed_id
                record["id"] = (
                    "candidate:citation:"
                    + _services()
                    .hashlib.sha1(
                        _services().candidate_identity(record).encode("utf-8")
                    )
                    .hexdigest()[:20]
                )
                candidates.append(record)
        candidates = _services()._unique_agent_papers(candidates, limit=60)
        history = {
            "new": [],
            "changed": [],
            "repeated": [],
            "new_count": 0,
            "changed_count": 0,
            "repeated_count": 0,
        }
        goal = payload.get("goal") or {}
        if goal.get("id") and candidates:
            with _services().DB_LOCK, _services().db_session() as db:
                goal_id = _services().clean_text(goal.get("id"), 80)
                if db.execute(
                    "SELECT 1 FROM research_goals WHERE id=?", (goal_id,)
                ).fetchone():
                    history = _services()._record_goal_paper_history(
                        db,
                        goal_id,
                        _services().clean_text(payload.get("run_id", ""), 80),
                        candidates,
                    )
        return {
            "status": (
                "completed" if candidates else ("skipped" if not errors else "partial")
            ),
            "expanded": bool(candidates),
            "papers": candidates,
            "candidate_ids": [item.get("id") for item in candidates],
            "seed_count": len(seed_items),
            "history": history,
            "errors": errors,
            "warning": "引用扩展只返回候选快照，不会自动写入文献库",
        }
    if tool_name == "track_follow_up":
        previous = payload.get("previous") or {}
        # Histories are recorded at the merge and bounded expansion steps.  A
        # follow-up report intentionally compares stable paper identities and
        # never treats a provider's transient candidate ID as new work.
        buckets = {"new": {}, "changed": {}, "repeated": {}}
        for source_key in ("merge_versions", "expand_search", "expand_citations"):
            history = (
                previous.get(source_key, {}).get("history", {})
                if isinstance(previous.get(source_key), dict)
                else {}
            )
            for bucket in buckets:
                for item in (
                    history.get(bucket, [])
                    if isinstance(history.get(bucket), list)
                    else []
                ):
                    key = _services().clean_text(
                        item.get("paper_key"), 300
                    ) or _services().title_key(item.get("title", ""))
                    if key:
                        buckets[bucket][key] = item
        return {
            "artifact_type": "follow_up_digest",
            "status": "completed",
            "new_papers": list(buckets["new"].values())[:100],
            "changed_papers": list(buckets["changed"].values())[:100],
            "repeated_papers": list(buckets["repeated"].values())[:100],
            "new_count": len(buckets["new"]),
            "changed_count": len(buckets["changed"]),
            "repeated_count": len(buckets["repeated"]),
            "summary": "本次运行只报告相对研究目标历史新增或发生变化的论文；重复命中保留为审计信息。",
        }
    if tool_name == "generate_reading_route":
        papers = (
            payload.get("papers") if isinstance(payload.get("papers"), list) else []
        )
        claims = (
            payload.get("claims") if isinstance(payload.get("claims"), list) else []
        )
        claim_by_paper = {}
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            for alias in (claim.get("paper_id"), claim.get("local_paper_id")):
                if alias:
                    claim_by_paper.setdefault(str(alias), []).append(claim)

        def _route_role(item: dict) -> tuple[int, str, str, str, list[str]]:
            aliases = {str(item.get("id") or ""), str(item.get("local_paper_id") or "")}
            own_claims = [
                claim for alias in aliases for claim in claim_by_paper.get(alias, [])
            ]
            types = {claim.get("claim_type") for claim in own_claims}
            year = int(item.get("year") or 0)
            recent = (
                year >= _services().datetime.now(_services().timezone.utc).year - 2
                if year
                else False
            )
            if "research_question" in types or (
                year
                and year < _services().datetime.now(_services().timezone.utc).year - 5
            ):
                return (
                    0,
                    "先修/奠基",
                    "先建立问题定义、术语和评价口径",
                    "读完后写出问题边界与至少三项比较维度",
                    ["问题定义是否一致？", "论文解决的范围是什么？"],
                )
            if "method" in types or not own_claims:
                return (
                    1,
                    "核心方法",
                    "拆解方法结构、关键假设和输入输出",
                    "画出方法流程并记录需要复现的设置",
                    ["方法依赖哪些假设？", "哪一步决定主要收益？"],
                )
            if recent:
                return (
                    2,
                    "最新进展",
                    "确认近期方法变化、数据集和指标趋势",
                    "更新研究地图并标记可迁移模块",
                    ["改进来自方法还是数据/协议？", "与基线是否公平？"],
                )
            if "limitation" in types:
                return (
                    3,
                    "反例/局限",
                    "寻找失败案例、适用边界和未解决问题",
                    "形成批判清单和后续实验假设",
                    ["结论在哪些条件下失效？", "局限是否有实验证据？"],
                )
            return (
                4,
                "复现与补充",
                "补齐相邻方法、实现和可复现信息",
                "整理复现 checklist 或补证任务",
                ["代码、数据和训练设置是否可获得？", "结果能否在同一协议下比较？"],
            )

        ordered = sorted(
            papers,
            key=lambda item: (
                _route_role(item)[0],
                -(item.get("relevance_score") or item.get("discovery_score") or 0),
                item.get("year") or 9999,
            ),
        )
        route = []
        for index, item in enumerate(ordered, 1):
            has_fulltext = bool(
                item.get("local_document_pages")
                or item.get("has_local_pdf")
                or item.get("pdf_url")
            )
            _priority, stage, reason, expected_output, questions = _route_role(item)
            route.append(
                {
                    "order": index,
                    "paper_id": item.get("id"),
                    "title": item.get("title"),
                    "stage": stage,
                    "reason": reason,
                    "reading_goal": reason,
                    "expected_output": expected_output,
                    "verification_questions": questions,
                    "prerequisites": (
                        "先掌握该方向的任务定义、数据集和评价指标"
                        if stage != "先修/奠基"
                        else "无；先建立共同术语和比较口径"
                    ),
                    "evidence_ready": has_fulltext,
                    "evidence_scope": (
                        "fulltext_available"
                        if has_fulltext
                        else ("abstract_only" if item.get("abstract") else "unverified")
                    ),
                }
            )
        return {
            "route": route,
            "route_count": len(route),
            "unreadable_count": sum(not item.get("evidence_ready") for item in route),
        }
    if tool_name == "compare_papers":
        papers = (
            payload.get("papers") if isinstance(payload.get("papers"), list) else []
        )
        claims = (
            payload.get("claims") if isinstance(payload.get("claims"), list) else []
        )
        claims_by_paper = {}
        for claim in claims:
            if isinstance(claim, dict):
                # A triage candidate keeps its transient candidate ID while
                # extracted Claims use the stable local paper ID.  Index both
                # aliases so a local full text is not reported as missing.
                for alias in (claim.get("paper_id"), claim.get("local_paper_id")):
                    alias = str(alias or "")
                    if alias:
                        claims_by_paper.setdefault(alias, []).append(claim)
        dimensions = [
            "研究问题",
            "任务与适用边界",
            "方法",
            "关键假设",
            "数据集",
            "基线与实验",
            "评价指标可比性",
            "结果",
            "局限",
            "复现线索",
            "证据范围",
            "年份/来源",
            "开放获取",
        ]
        rows = []
        gaps = []
        for paper in papers:
            paper_id = str(paper.get("id") or "")
            aliases = [paper_id, str(paper.get("local_paper_id") or "")]
            paper_claims = []
            seen_claim_ids = set()
            for alias in aliases:
                for claim in claims_by_paper.get(alias, []):
                    claim_key = claim.get("id") or claim.get("claim_id") or id(claim)
                    if claim_key not in seen_claim_ids:
                        seen_claim_ids.add(claim_key)
                        paper_claims.append(claim)
            by_type = {}
            for claim in paper_claims:
                by_type.setdefault(claim.get("claim_type") or "other", []).append(
                    _services().clean_text(claim.get("claim_text", ""), 700)
                )
            all_claim_text = " ".join(
                value for values in by_type.values() for value in values
            )
            page_claims = [
                claim
                for claim in paper_claims
                if claim.get("page") not in (None, "", 0)
                or claim.get("evidence_scope") == "fulltext_page"
            ]
            metric_tokens = [
                "accuracy",
                "precision",
                "recall",
                "f1",
                "auc",
                "rmse",
                "mae",
                "性能",
                "准确率",
                "指标",
            ]
            metric_signal = any(
                token in all_claim_text.casefold() for token in metric_tokens
            )
            reproducibility_signal = any(
                token in all_claim_text.casefold()
                for token in (
                    "code",
                    "github",
                    "hyperparameter",
                    "learning rate",
                    "代码",
                    "开源",
                    "超参数",
                )
            )
            row = {
                "paper_id": paper_id,
                "title": paper.get("title"),
                "year": paper.get("year"),
                "venue": paper.get("venue"),
                "topic": paper.get("topic"),
                "relevance": paper.get("relevance_label"),
                "open_access": bool(paper.get("open_access")),
                "research_question": by_type.get("research_question", [])[:3],
                "method": by_type.get("method", [])[:3],
                "datasets": by_type.get("dataset", [])[:3],
                "results": by_type.get("result", [])[:3],
                "limitations": by_type.get("limitation", [])[:3],
                "evidence_scope": sorted(
                    {
                        claim.get("evidence_scope")
                        for claim in paper_claims
                        if claim.get("evidence_scope")
                    }
                ),
                "assumptions": by_type.get("assumption", [])[:3],
                "task_boundary": _services().clean_text(
                    paper.get("topic")
                    or (
                        by_type.get("research_question", [""])[0]
                        if by_type.get("research_question")
                        else ""
                    ),
                    500,
                ),
                "evidence_level": (
                    "page_verified"
                    if page_claims
                    else (
                        "abstract_only"
                        if any(
                            claim.get("evidence_scope") == "abstract"
                            for claim in paper_claims
                        )
                        else "unverified"
                    )
                ),
                "metric_comparability": (
                    "有可比指标线索，仍需核对数据集、划分和统计协议"
                    if metric_signal
                    else "未定位统一指标线索，不能直接比较数值"
                ),
                "reproducibility_signals": (
                    "已定位实现/训练设置线索"
                    if reproducibility_signal
                    else "未定位实现或训练设置线索"
                ),
                "applicability_boundary": "需要结合任务、数据集和局限 Claim 判断，不能仅按相关性迁移",
            }
            row["missing_fields"] = [
                field
                for field, value in (
                    ("research_question", row["research_question"]),
                    ("method", row["method"]),
                    ("datasets", row["datasets"]),
                    ("baselines", by_type.get("baseline", [])),
                    ("results", row["results"]),
                    ("limitations", row["limitations"]),
                )
                if not value
            ]
            if row["missing_fields"]:
                gaps.append(
                    {
                        "paper_id": paper_id,
                        "title": paper.get("title"),
                        "fields": row["missing_fields"],
                        "why": "该字段没有可回溯的 Claim 或页级证据，不能用摘要印象补齐",
                        "next_step": "回到全文的对应章节、表格、附录或代码/数据说明核验",
                    }
                )
            rows.append(row)
        return {
            "dimensions": dimensions,
            "rows": rows,
            "row_count": len(rows),
            "gaps": gaps[:100],
            "claim_count": len(claims),
        }
    if tool_name == "resolve_comparison_gaps":
        papers = (
            payload.get("papers") if isinstance(payload.get("papers"), list) else []
        )
        claims = (
            payload.get("claims") if isinstance(payload.get("claims"), list) else []
        )
        comparison = (
            payload.get("comparison")
            if isinstance(payload.get("comparison"), dict)
            else {}
        )
        goal = payload.get("goal") or {}
        run_id = _services().clean_text(payload.get("run_id", ""), 80)
        gaps = (
            comparison.get("gaps") if isinstance(comparison.get("gaps"), list) else []
        )
        # A gap is counted per paper/field.  This keeps the tool bounded even
        # when a broad comparison contains hundreds of candidates.
        work = []
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            paper_id = _services().clean_text(gap.get("paper_id", ""), 200)
            fields = gap.get("fields") if isinstance(gap.get("fields"), list) else []
            for field in fields:
                field = _services().clean_text(field, 80)
                if field and paper_id:
                    work.append((paper_id, field))
        work = work[:30]
        paper_by_id = {}
        for paper in papers:
            if not isinstance(paper, dict):
                continue
            for alias in (paper.get("id"), paper.get("local_paper_id")):
                alias = _services().clean_text(alias, 200)
                if alias:
                    paper_by_id[alias] = paper
        existing_fingerprints = set()
        for claim in claims:
            if isinstance(claim, dict):
                existing_fingerprints.add(
                    "|".join(
                        _services().clean_text(claim.get(key, ""), 1200).casefold()
                        for key in ("paper_id", "claim_type", "claim_text")
                    )
                )
        field_terms = {
            "method": [
                "method",
                "methodology",
                "approach",
                "architecture",
                "algorithm",
                "模型",
                "方法",
            ],
            "datasets": [
                "dataset",
                "benchmark",
                "data set",
                "corpus",
                "sample",
                "数据集",
                "基准",
                "样本",
            ],
            "results": [
                "result",
                "results",
                "accuracy",
                "performance",
                "f1",
                "auc",
                "improv",
                "outperform",
                "结果",
                "性能",
                "实验",
            ],
            "baselines": ["baseline", "comparison", "compared with", "基线", "比较"],
            "research_question": [
                "objective",
                "research question",
                "we address",
                "研究问题",
                "目标",
            ],
            "limitations": [
                "limitation",
                "failure",
                "future work",
                "局限",
                "不足",
                "失败",
                "未来工作",
            ],
        }
        attempted = 0
        resolved_count = 0
        evidence_candidates = []
        new_claims = []
        no_local = 0
        now = _services().now_iso()
        with _services().DB_LOCK, _services().db_session() as db:
            for candidate_id, field in work:
                attempted += 1
                paper = paper_by_id.get(candidate_id) or {}
                local_id = _services().clean_text(
                    paper.get("local_paper_id") or candidate_id, 200
                )
                # Candidate IDs are snapshots only.  The Agent must never
                # silently download or import a PDF just to fill a matrix.
                if local_id.startswith("candidate:"):
                    no_local += 1
                    continue
                page_count = int(
                    db.execute(
                        "SELECT COUNT(*) FROM document_pages WHERE paper_id=? AND quality='good'",
                        (local_id,),
                    ).fetchone()[0]
                )
                if page_count == 0:
                    no_local += 1
                    continue
                terms = field_terms.get(field, [field])
                matches = []
                matched_term = ""
                for term in terms:
                    matches = _services().search_document_pages(local_id, term, 3)
                    if matches:
                        matched_term = term
                        break
                if not matches:
                    continue
                for page in matches[:1]:
                    page_number = int(page.get("page_number") or 0)
                    if not page_number:
                        continue
                    source_row = db.execute(
                        "SELECT text,text_sha256 FROM document_pages WHERE paper_id=? AND page_number=? ORDER BY extracted_at DESC LIMIT 1",
                        (local_id, page_number),
                    ).fetchone()
                    full_text = _services().clean_text(
                        source_row["text"] if source_row else "", 500000
                    )
                    if not full_text:
                        continue
                    # `search_document_pages` intentionally returns a UI
                    # snippet and may insert ellipses.  Evidence must be an
                    # exact contiguous excerpt from the normalized page so a
                    # later verifier can locate it again.
                    normalized_text = _services().re.sub(r"\s+", " ", full_text).strip()
                    needle = _services().clean_text(matched_term, 120).casefold()
                    position = normalized_text.casefold().find(needle) if needle else -1
                    if position >= 0:
                        quote = normalized_text[
                            max(0, position - 180) : min(
                                len(normalized_text), position + len(needle) + 420
                            )
                        ]
                    else:
                        quote = normalized_text[:700]
                    quote = _services().clean_text(quote, 700)
                    if not quote:
                        continue
                    claim_type = (
                        "dataset"
                        if field == "datasets"
                        else (
                            "result"
                            if field == "results"
                            else (
                                "limitation"
                                if field == "limitations"
                                else (
                                    "research_question"
                                    if field == "research_question"
                                    else "method"
                                )
                            )
                        )
                    )
                    claim_text = f"比较矩阵 {field}：原文摘录：{quote}"
                    fingerprint = "|".join(
                        _services().clean_text(value, 1200).casefold()
                        for value in (local_id, claim_type, claim_text)
                    )
                    if fingerprint in existing_fingerprints:
                        continue
                    claim_id = str(_services().uuid.uuid4())
                    evidence_id = str(_services().uuid.uuid4())
                    source_hash = _services().clean_text(
                        (
                            source_row["text_sha256"]
                            if source_row
                            else page.get("text_sha256", "")
                        ),
                        120,
                    )
                    section = _services()._infer_page_section(full_text)
                    db.execute(
                        "INSERT INTO research_claims(id,goal_id,run_id,paper_id,claim_text,claim_type,status,confidence,evidence_scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            claim_id,
                            _services().clean_text(goal.get("id", ""), 80),
                            run_id,
                            local_id,
                            claim_text,
                            claim_type,
                            "page_quote_matched",
                            "medium",
                            "fulltext_page",
                            now,
                            now,
                        ),
                    )
                    db.execute(
                        "INSERT INTO evidence_spans(id,claim_id,paper_id,page_number,section,quote,evidence_type,verification_status,source_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            evidence_id,
                            claim_id,
                            local_id,
                            page_number,
                            section,
                            quote,
                            "page_text",
                            "quote_match_page_text",
                            source_hash,
                            now,
                        ),
                    )
                    item = {
                        "id": claim_id,
                        "paper_id": local_id,
                        "paper_title": paper.get("title", ""),
                        "claim_text": claim_text,
                        "claim_type": claim_type,
                        "evidence_id": evidence_id,
                        "page": page_number,
                        "section": section,
                        "source_sha256": source_hash,
                        "evidence_scope": "fulltext_page",
                        "status": "page_quote_matched",
                        "comparison_field": field,
                    }
                    new_claims.append(item)
                    evidence_candidates.append(
                        {
                            "claim_id": claim_id,
                            "paper_id": local_id,
                            "field": field,
                            "page": page_number,
                            "section": section,
                            "evidence_id": evidence_id,
                            "quote": quote,
                            "source_sha256": source_hash,
                        }
                    )
                    existing_fingerprints.add(fingerprint)
                    resolved_count += 1
        remaining_count = max(0, len(work) - resolved_count)
        if not work:
            status = "no_gaps"
        elif resolved_count:
            status = "resolved" if remaining_count == 0 else "partial"
        elif no_local == len(work):
            status = "no_local_fulltext"
        else:
            status = "no_match"
        return {
            "attempted": attempted,
            "resolved_count": resolved_count,
            "remaining_count": remaining_count,
            "claims": new_claims,
            "evidence_candidates": evidence_candidates,
            "status": status,
            "warning": "只使用已有本地页级文本；候选论文不会自动下载或写入文献库",
        }
    if tool_name == "generate_research_brief":
        goal = payload.get("goal") or {}
        triage = payload.get("triage") or {}
        papers = (
            triage.get("candidates")
            if isinstance(triage.get("candidates"), list)
            else []
        )
        previous = payload.get("previous") or {}
        expanded = previous.get("expand_search", {}).get("papers", [])
        cited = previous.get("expand_citations", {}).get("papers", [])
        papers = _services()._unique_agent_papers(
            papers,
            expanded if isinstance(expanded, list) else [],
            cited if isinstance(cited, list) else [],
        )
        grouped = {}
        for paper in papers:
            key = paper.get("topic") or paper.get("venue") or "未分类"
            grouped[key] = grouped.get(key, 0) + 1
        claims = _services()._unique_agent_claims(
            previous.get("extract_claims", {}).get("claims", []),
            previous.get("resolve_comparison_gaps", {}).get("claims", []),
        )
        verification = (
            payload.get("verification")
            if isinstance(payload.get("verification"), dict)
            else (
                previous.get("verify_comparison_evidence")
                or previous.get("verify_evidence_refined")
                or previous.get("verify_evidence", {})
            )
        )
        critique = (
            payload.get("critique")
            if isinstance(payload.get("critique"), dict)
            else (
                previous.get("critique_claims_final")
                or previous.get("critique_claims_refined")
                or previous.get("critique_claims", {})
            )
        )
        coverage = (
            (payload.get("previous") or {})
            .get("evaluate_coverage", {})
            .get("coverage", {})
        )
        merge = previous.get("merge_versions", {})
        history = merge.get("history", {}) if isinstance(merge, dict) else {}
        artifact_type = (
            "follow_up_digest"
            if goal.get("workflow") == "follow_up"
            else (
                "research_map"
                if goal.get("workflow") == "research_map"
                else "evidence_brief"
            )
        )
        route_output = (
            previous.get("reading_route")
            if isinstance(previous.get("reading_route"), dict)
            else _services().call_agent_tool(
                "generate_reading_route", {"papers": papers}
            )
        )
        comparison_output = (
            previous.get("refresh_comparison")
            if isinstance(previous.get("refresh_comparison"), dict)
            else (
                previous.get("compare_candidates")
                if isinstance(previous.get("compare_candidates"), dict)
                else _services().call_agent_tool(
                    "compare_papers", {"papers": papers, "claims": claims}
                )
            )
        )
        card_draft = (
            previous.get("draft_reading_card")
            if isinstance(previous.get("draft_reading_card"), dict)
            else {}
        )
        paper_overview = (
            previous.get("paper_analysis_overview", {})
            if isinstance(previous.get("paper_analysis_overview"), dict)
            else {}
        )
        artifact = {
            "artifact_type": artifact_type,
            "title": goal.get("name", "研究目标") + " · Agent 研究产物",
            "overview": "基于候选快照、版本关系、页级证据和单篇论文专业分析生成的可追溯研究产物。候选论文尚未自动写入文献库，需由你选择导入。",
            "coverage": {
                "candidate_count": len(papers),
                "topic_groups": grouped,
                "missing_abstract": sum(not bool(p.get("abstract")) for p in papers),
                "missing_fulltext": sum(
                    not bool(p.get("pdf_url") or p.get("has_local_pdf")) for p in papers
                ),
                **coverage,
            },
            "fulltext_plan": previous.get("prepare_fulltext", {}),
            "reading_route": route_output.get("route", []),
            "comparison": comparison_output,
            "comparison_gap_resolution": previous.get("resolve_comparison_gaps", {}),
            "paper_analysis_overview": paper_overview,
            "reading_card_draft": card_draft,
            "version_groups": merge.get("groups", []),
            "candidates": papers,
            "claims": claims[:50],
            "evidence_status": verification,
            "critique": critique,
            "new_papers": history.get("new", []),
            "changed_papers": history.get("changed", []),
            "repeated_papers": history.get("repeated", []),
            "uncertainties": [
                "候选结果基于元数据和摘要；摘要匹配只证明引文存在于摘要，不证明全文结论",
                "未获取全文的论文不能视为已验证证据",
                "单篇分析的正向结论必须回到页码或摘要引用；未定位内容会作为缺口保留",
                "结果来自当前来源快照，后续运行可能发现新版本或重复记录",
            ],
        }
        run = payload.get("run") or {}
        if (
            (run.get("budget") or {}).get("consent_external") is True
            and _services().model_settings().get("enabled")
            and _services().model_api_key()
        ):
            # The model receives the local, evidence-scoped analysis as well as
            # metadata.  This keeps synthesis useful after full-text review
            # without allowing it to silently promote an abstract-only claim.
            compact_overviews = []
            candidate_scope = {}
            candidate_refs = {}
            for overview in (
                paper_overview.get("paper_overviews", [])
                if isinstance(paper_overview, dict)
                else []
            )[:16]:
                if not isinstance(overview, dict):
                    continue
                analysis = (
                    overview.get("analysis")
                    if isinstance(overview.get("analysis"), dict)
                    else {}
                )
                overview_id = _services().clean_text(overview.get("paper_id", ""), 200)
                candidate = next(
                    (
                        item
                        for item in papers[:16]
                        if overview_id
                        in {
                            _services().clean_text(item.get("id", ""), 200),
                            _services().clean_text(item.get("local_paper_id", ""), 200),
                        }
                    ),
                    None,
                )
                candidate_id = (
                    _services().clean_text(candidate.get("id", ""), 200)
                    if candidate
                    else overview_id
                )
                candidate_scope[candidate_id] = overview.get(
                    "source_scope", "unverified"
                )
                refs = []
                for value in analysis.values():
                    if isinstance(value, dict):
                        refs.extend(
                            item
                            for item in (value.get("evidence_refs") or [])
                            if isinstance(item, dict)
                        )
                deduped_refs = []
                seen_refs = set()
                for ref in refs:
                    key = (ref.get("page"), ref.get("section"), ref.get("quote"))
                    if key in seen_refs:
                        continue
                    seen_refs.add(key)
                    deduped_refs.append(
                        {
                            "page": ref.get("page"),
                            "section": _services().clean_text(
                                ref.get("section", ""), 80
                            ),
                            "quote": _services().clean_text(ref.get("quote", ""), 360),
                        }
                    )
                candidate_refs[candidate_id] = deduped_refs[:8]
                compact_overviews.append(
                    {
                        "candidate_id": candidate_id,
                        "title": overview.get("title", ""),
                        "source_scope": overview.get("source_scope", "unverified"),
                        "analysis": {
                            key: _services().clean_text(
                                (analysis.get(key) or {}).get("text", ""), 900
                            )
                            for key in (
                                "problem",
                                "method",
                                "assumptions",
                                "data_and_experiments",
                                "results",
                                "limitations",
                            )
                        },
                        "evidence_refs": deduped_refs[:8],
                        "critical_risks": [
                            _services().clean_text(item.get("text", ""), 500)
                            for item in (overview.get("critical_risks") or [])[:4]
                            if isinstance(item, dict)
                        ],
                        "open_questions": [
                            _services().clean_text(item, 400)
                            for item in (overview.get("open_questions") or [])[:6]
                        ],
                    }
                )
            model_input = {
                "objective": goal.get("objective", ""),
                "focus": goal.get("focus", []),
                "papers": [
                    {
                        "id": paper.get("id"),
                        "title": paper.get("title"),
                        "year": paper.get("year"),
                        "abstract": _services().clean_text(
                            paper.get("abstract", ""), 1200
                        ),
                    }
                    for paper in papers[:16]
                ],
                "evidence_scoped_overviews": compact_overviews,
                "comparison": {
                    "rows": (comparison_output.get("rows") or [])[:16],
                    "gaps": (comparison_output.get("gaps") or [])[:20],
                },
            }
            prompt = "你是严谨的研究生论文综合助理。只根据输入生成严格 JSON: {synthesis:string, themes:[{text:string,candidate_ids:[string],reason:string}], gaps:[{text:string,candidate_ids:[string],reason:string}], cited_candidate_ids:[string]}。source_scope=abstract_only 只能写摘要支持的线索；source_scope=fulltext_page 也只能写给定的分析和逐字证据，不得补造页码、数值、实验、数据集或因果结论。每个主题、缺口和综合判断必须精确列出支持它的候选 ID；没有具体支持 ID 的项目不要输出。证据不足必须明确写‘证据不足，需回到原文核验’，不能用泛泛常识填空。不要自行生成引文或证据范围，服务端会根据 ID 附加已经保存的真实证据。"
            try:
                draft = _services().model_json_request(prompt, model_input, timeout=90)
                allowed_ids = {paper.get("id") for paper in papers}
                cited_ids = (
                    [
                        item
                        for item in draft.get("cited_candidate_ids", [])
                        if item in allowed_ids
                    ]
                    if isinstance(draft.get("cited_candidate_ids"), list)
                    else []
                )

                def normalize_supported_items(values):
                    normalized, notes = [], []
                    for value in values[:20] if isinstance(values, list) else []:
                        item = (
                            value
                            if isinstance(value, dict)
                            else {"text": value, "candidate_ids": cited_ids}
                        )
                        text_value = _services().clean_text(item.get("text", ""), 800)
                        support_ids = (
                            list(
                                dict.fromkeys(
                                    candidate_id
                                    for candidate_id in item.get("candidate_ids", [])
                                    if candidate_id in allowed_ids
                                )
                            )
                            if isinstance(item.get("candidate_ids"), list)
                            else []
                        )
                        if not text_value or not support_ids:
                            continue
                        normalized.append(text_value)
                        for candidate_id in support_ids:
                            scope = candidate_scope.get(candidate_id, "unverified")
                            for ref in candidate_refs.get(candidate_id, [])[:3]:
                                note = {
                                    "candidate_id": candidate_id,
                                    "scope": scope,
                                    **ref,
                                }
                                if note not in notes:
                                    notes.append(note)
                    return normalized, notes

                themes, theme_evidence = normalize_supported_items(draft.get("themes"))
                gaps, gap_evidence = normalize_supported_items(draft.get("gaps"))
                synthesis = _services().clean_text(draft.get("synthesis", ""), 12000)
                synthesis_evidence = []
                for candidate_id in cited_ids:
                    for ref in candidate_refs.get(candidate_id, [])[:2]:
                        note = {
                            "candidate_id": candidate_id,
                            "scope": candidate_scope.get(candidate_id, "unverified"),
                            **ref,
                        }
                        if note not in synthesis_evidence:
                            synthesis_evidence.append(note)
                artifact["model_draft"] = {
                    "synthesis": synthesis,
                    "themes": themes,
                    "gaps": gaps,
                    "theme_evidence": theme_evidence[:30],
                    "gap_evidence": gap_evidence[:30],
                    "synthesis_evidence": synthesis_evidence[:24],
                    "cited_candidate_ids": cited_ids,
                    "uncited_claim_count": int(bool(synthesis)) if not cited_ids else 0,
                    "unsupported_theme_count": (
                        max(0, len(draft.get("themes", [])) - len(themes))
                        if isinstance(draft.get("themes"), list)
                        else 0
                    ),
                    "unsupported_gap_count": (
                        max(0, len(draft.get("gaps", [])) - len(gaps))
                        if isinstance(draft.get("gaps"), list)
                        else 0
                    ),
                    "model": _services().model_settings().get("model", ""),
                }
            except Exception as exc:
                artifact["model_draft_error"] = _services().clean_text(
                    _services().model_error_detail(exc, _services().model_api_key()),
                    500,
                )
        return artifact
    if tool_name == "draft_reading_card":
        paper = payload.get("paper") if isinstance(payload.get("paper"), dict) else {}
        return _services()._agent_fulltext_reading_card(paper, payload)
    if tool_name == "extract_claims":
        papers = (
            payload.get("papers") if isinstance(payload.get("papers"), list) else []
        )
        goal = payload.get("goal") or {}
        run_id = _services().clean_text(payload.get("run_id", ""), 80)
        claims = []
        now = _services().now_iso()
        with _services().DB_LOCK, _services().db_session() as db:
            for paper in papers[:50]:
                local_paper_id = _services().clean_text(
                    paper.get("local_paper_id", ""), 200
                )
                page_rows = (
                    db.execute(
                        "SELECT page_number,text,text_sha256 FROM document_pages WHERE paper_id=? AND quality<>'empty' AND trim(text)<>'' ORDER BY page_number LIMIT 160",
                        (local_paper_id,),
                    ).fetchall()
                    if local_paper_id
                    else []
                )
                if page_rows:
                    # Cover the paper's structure instead of taking only the
                    # first three pages.  One representative page per
                    # section plus pages containing result/ablation/figure
                    # cues gives the critic evidence from methods, results and
                    # limitations while keeping the claim budget bounded.
                    selected_pages = []
                    seen_sections = set()
                    cue_terms = (
                        "result",
                        "experiment",
                        "ablation",
                        "limitation",
                        "conclusion",
                        "figure",
                        "table",
                        "结果",
                        "实验",
                        "消融",
                        "局限",
                        "结论",
                        "图",
                        "表",
                    )
                    for row in page_rows:
                        section = _services()._infer_page_section(row["text"])
                        normalized = (
                            _services()
                            .re.sub(
                                r"\s+", " ", _services().clean_text(row["text"], 5000)
                            )
                            .strip()
                        )
                        if section not in seen_sections or any(
                            term in normalized.casefold() for term in cue_terms
                        ):
                            selected_pages.append(row)
                            seen_sections.add(section)
                        if len(selected_pages) >= 12:
                            break
                    quote_data = []
                    for row in selected_pages:
                        normalized = (
                            _services()
                            .re.sub(
                                r"\s+", " ", _services().clean_text(row["text"], 5000)
                            )
                            .strip()
                        )
                        quote = normalized[:700]
                        for cue in cue_terms:
                            position = normalized.casefold().find(cue.casefold())
                            if position >= 0:
                                quote = normalized[
                                    max(0, position - 180) : position + 520
                                ]
                                break
                        quote_data.append(
                            {
                                "quote": quote[:700],
                                "page": int(row["page_number"]),
                                "sha256": row["text_sha256"],
                                "section": _services()._infer_page_section(row["text"]),
                            }
                        )
                    scope = "fulltext_page"
                else:
                    abstract = _services().clean_text(paper.get("abstract", ""), 30000)
                    if not abstract:
                        continue
                    quote_data = [
                        {
                            "quote": _services()
                            .re.sub(r"\s+", " ", abstract)
                            .strip()[:700],
                            "page": None,
                            "sha256": _services()
                            .hashlib.sha256(abstract.encode("utf-8"))
                            .hexdigest(),
                            "section": "abstract",
                        }
                    ]
                    scope = "abstract"
                if not quote_data:
                    continue
                for evidence in quote_data[:8]:
                    quote = evidence["quote"]
                    if not quote:
                        continue
                    claim_id = str(_services().uuid.uuid4())
                    lower_quote = quote.casefold()
                    if any(
                        term in lower_quote
                        for term in (
                            "limitation",
                            "failure",
                            "future work",
                            "局限",
                            "不足",
                            "失败",
                        )
                    ):
                        claim_type = "limitation"
                    elif any(
                        term in lower_quote
                        for term in (
                            "dataset",
                            "benchmark",
                            "data set",
                            "数据集",
                            "样本",
                        )
                    ):
                        claim_type = "dataset"
                    elif any(
                        term in lower_quote
                        for term in (
                            "outperform",
                            "accuracy",
                            "f1",
                            "auc",
                            "实验",
                            "结果",
                            "improv",
                        )
                    ):
                        claim_type = "result"
                    elif any(
                        term in lower_quote
                        for term in (
                            "method",
                            "approach",
                            "architecture",
                            "model",
                            "方法",
                            "模型",
                        )
                    ):
                        claim_type = "method"
                    else:
                        claim_type = "research_question"
                    claim_text = (
                        "原文摘录：" if evidence["page"] else "摘要摘录："
                    ) + quote
                    claim_status = "needs_verification"
                    db.execute(
                        "INSERT INTO research_claims(id,goal_id,run_id,paper_id,claim_text,claim_type,status,confidence,evidence_scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            claim_id,
                            goal["id"],
                            run_id,
                            _services().clean_text(
                                local_paper_id or paper.get("id", ""), 200
                            ),
                            claim_text,
                            claim_type,
                            claim_status,
                            "medium",
                            scope,
                            now,
                            now,
                        ),
                    )
                    evidence_id = str(_services().uuid.uuid4())
                    db.execute(
                        "INSERT INTO evidence_spans(id,claim_id,paper_id,page_number,section,quote,evidence_type,verification_status,source_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            evidence_id,
                            claim_id,
                            _services().clean_text(
                                local_paper_id or paper.get("id", ""), 200
                            ),
                            evidence["page"],
                            evidence["section"],
                            quote,
                            "page_text" if evidence["page"] else "abstract_text",
                            "unverified",
                            evidence["sha256"],
                            now,
                        ),
                    )
                    claims.append(
                        {
                            "id": claim_id,
                            "paper_id": local_paper_id or paper.get("id"),
                            "paper_title": paper.get("title"),
                            "claim_text": claim_text,
                            "claim_type": claim_type,
                            "evidence_id": evidence_id,
                            "page": evidence["page"],
                            "evidence_scope": scope,
                            "status": claim_status,
                        }
                    )
                    if len(claims) >= 240:
                        break
                if len(claims) >= 240:
                    break
        return {
            "claims": claims,
            "claim_count": len(claims),
            "claim_types": {
                kind: sum(item.get("claim_type") == kind for item in claims)
                for kind in {item.get("claim_type") for item in claims}
            },
            "page_scoped": sum(item.get("page") is not None for item in claims),
            "abstract_only": sum(item.get("page") is None for item in claims),
            "warning": "摘要证据不能替代全文页码证据；页码摘录仅证明文本命中，不证明研究结论正确",
        }
    if tool_name == "resolve_evidence_gaps":
        previous = payload.get("previous") or {}
        claims = previous.get("extract_claims", {}).get("claims", [])
        verification = payload.get("verification") or previous.get(
            "verify_evidence", {}
        )
        result_rows = (
            verification.get("results") if isinstance(verification, dict) else []
        )
        unresolved_ids = {
            row.get("claim_id")
            for row in result_rows
            if isinstance(row, dict)
            and row.get("status")
            in {"abstract_only", "evidence_unverified", "quote_not_found"}
        }
        if not unresolved_ids:
            unresolved_ids = {
                claim.get("id")
                for claim in claims
                if claim.get("evidence_scope") == "abstract"
            }
        attempted = resolved = 0
        evidence_candidates = []
        now = _services().now_iso()
        with _services().DB_LOCK, _services().db_session() as db:
            for claim in claims[:50]:
                claim_id = _services().clean_text(claim.get("id", ""), 80)
                if not claim_id or claim_id not in unresolved_ids:
                    continue
                paper_id = _services().clean_text(claim.get("paper_id", ""), 200)
                if not paper_id or paper_id.startswith("candidate:"):
                    continue
                attempted += 1
                terms = [
                    term
                    for term in _services().re.findall(
                        r"[A-Za-z0-9][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}",
                        _services().clean_text(claim.get("claim_text", ""), 1000),
                    )
                    if term.casefold() not in {"原文摘录", "摘要摘录", "claim"}
                ]
                query = " ".join(dict.fromkeys(terms[:5]))
                if not query:
                    continue
                pages = _services().search_document_pages(paper_id, query, 3)
                if not pages:
                    # Search one discriminative term when an AND-style query
                    # is too strict for a sparse or OCR-derived page index.
                    pages = (
                        _services().search_document_pages(paper_id, terms[0], 3)
                        if terms
                        else []
                    )
                for page in pages[:2]:
                    page_number = int(page.get("page_number") or page.get("page") or 0)
                    source_row = db.execute(
                        "SELECT text,text_sha256 FROM document_pages WHERE paper_id=? AND page_number=? ORDER BY extracted_at DESC LIMIT 1",
                        (paper_id, page_number),
                    ).fetchone()
                    source_text = _services().clean_text(
                        source_row["text"] if source_row else page.get("snippet", ""),
                        500000,
                    )
                    quote = _services().clean_text(source_text[:700], 1000)
                    if not page_number or not quote:
                        continue
                    page_hash = _services().clean_text(
                        (
                            source_row["text_sha256"]
                            if source_row
                            else page.get("text_sha256", "")
                        ),
                        100,
                    )
                    evidence_id = str(_services().uuid.uuid4())
                    db.execute(
                        "INSERT INTO evidence_spans(id,claim_id,paper_id,page_number,section,quote,evidence_type,verification_status,source_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            evidence_id,
                            claim_id,
                            paper_id,
                            page_number,
                            _services().clean_text(
                                page.get("section") or "PDF 页面文本", 200
                            ),
                            quote[:700],
                            "page_text",
                            "unverified",
                            page_hash,
                            now,
                        ),
                    )
                    evidence_candidates.append(
                        {
                            "claim_id": claim_id,
                            "paper_id": paper_id,
                            "page": page_number,
                            "evidence_id": evidence_id,
                            "quote": quote[:700],
                        }
                    )
                    resolved += 1
        remaining = max(
            0,
            len(unresolved_ids)
            - len({item["claim_id"] for item in evidence_candidates}),
        )
        return {
            "round": int(payload.get("round", 1) or 1),
            "attempted": attempted,
            "resolved_count": resolved,
            "remaining_count": remaining,
            "evidence_candidates": evidence_candidates,
            "status": (
                "resolved"
                if resolved
                else ("no_local_fulltext" if attempted == 0 else "no_match")
            ),
        }
    if tool_name == "verify_evidence":
        goal = payload.get("goal") or {}
        run_id = _services().clean_text(payload.get("run_id", ""), 80)
        verified = needs_fulltext = missing = 0
        results = []
        with _services().DB_LOCK, _services().db_session() as db:
            rows = db.execute(
                "SELECT c.id,c.paper_id,c.claim_text,c.claim_type,e.id AS evidence_id,e.quote,e.section,e.page_number,e.source_sha256 FROM research_claims c JOIN evidence_spans e ON e.claim_id=c.id WHERE c.run_id=? ORDER BY c.created_at",
                (run_id,),
            ).fetchall()
            grouped = {}
            for row in rows:
                grouped.setdefault(row["id"], []).append(row)
            for claim_id, claim_rows in grouped.items():
                page_found = abstract_found = False
                page_evidence_ids, abstract_evidence_ids, missing_evidence_ids = (
                    [],
                    [],
                    [],
                )
                for row in claim_rows:
                    quote = _services().re.sub(r"\s+", " ", row["quote"] or "").strip()
                    if row["section"] == "abstract":
                        candidate_row = db.execute(
                            "SELECT record_json FROM discovery_candidates WHERE id=?",
                            (row["paper_id"],),
                        ).fetchone()
                        paper = (
                            _services()._json_value(candidate_row[0], {})
                            if candidate_row
                            else {}
                        )
                        haystack = (
                            _services()
                            .re.sub(
                                r"\s+",
                                " ",
                                _services().clean_text(
                                    paper.get("abstract", ""), 30000
                                ),
                            )
                            .strip()
                        )
                        if not haystack and quote:
                            haystack = quote
                        found = bool(quote and quote.casefold() in haystack.casefold())
                        if found:
                            abstract_found = True
                            abstract_evidence_ids.append(row["evidence_id"])
                            db.execute(
                                "UPDATE evidence_spans SET verification_status='quote_match_abstract_only' WHERE id=?",
                                (row["evidence_id"],),
                            )
                        else:
                            missing_evidence_ids.append(row["evidence_id"])
                            db.execute(
                                "UPDATE evidence_spans SET verification_status='quote_not_found' WHERE id=?",
                                (row["evidence_id"],),
                            )
                    else:
                        page_row = db.execute(
                            "SELECT text,text_sha256 FROM document_pages WHERE paper_id=? AND page_number=?",
                            (row["paper_id"], row["page_number"]),
                        ).fetchone()
                        haystack = (
                            _services()
                            .re.sub(
                                r"\s+",
                                " ",
                                _services().clean_text(page_row["text"], 500000),
                            )
                            .strip()
                            if page_row
                            else ""
                        )
                        found = bool(quote and quote.casefold() in haystack.casefold())
                        if found:
                            page_found = True
                            page_evidence_ids.append(row["evidence_id"])
                            db.execute(
                                "UPDATE evidence_spans SET verification_status='quote_match_page_text' WHERE id=?",
                                (row["evidence_id"],),
                            )
                        else:
                            missing_evidence_ids.append(row["evidence_id"])
                            db.execute(
                                "UPDATE evidence_spans SET verification_status='quote_not_found' WHERE id=?",
                                (row["evidence_id"],),
                            )
                if page_found:
                    claim_status, evidence_status = (
                        "page_quote_matched",
                        "quote_match_page_text",
                    )
                    verified += 1
                elif abstract_found:
                    claim_status, evidence_status = (
                        "abstract_only",
                        "quote_match_abstract_only",
                    )
                    verified += 1
                    needs_fulltext += 1
                else:
                    claim_status, evidence_status = (
                        "evidence_unverified",
                        "quote_not_found",
                    )
                    missing += 1
                db.execute(
                    "UPDATE research_claims SET status=?,updated_at=? WHERE id=?",
                    (claim_status, _services().now_iso(), claim_id),
                )
                results.append(
                    {
                        "claim_id": claim_id,
                        "status": claim_status,
                        "evidence_status": evidence_status,
                        "page_evidence_count": len(page_evidence_ids),
                        "abstract_evidence_count": len(abstract_evidence_ids),
                        "unmatched_evidence_count": len(missing_evidence_ids),
                    }
                )
        return {
            "claims_checked": len(results),
            "abstract_quote_matches": verified,
            "needs_fulltext": needs_fulltext,
            "evidence_not_found": missing,
            "results": results,
            "goal_id": goal.get("id"),
        }
    if tool_name == "critique_claims":
        goal = payload.get("goal") or {}
        run_id = _services().clean_text(payload.get("run_id", ""), 80)
        claims = (
            payload.get("claims")
            if isinstance(payload.get("claims"), list)
            else (payload.get("previous") or {})
            .get("extract_claims", {})
            .get("claims", [])
        )
        previous = payload.get("previous") or {}
        verification = (
            payload.get("verification")
            or previous.get("verify_evidence_refined")
            or previous.get("verify_evidence", {})
        )
        verification_by_id = {
            item.get("claim_id"): item
            for item in (verification.get("results") or [])
            if isinstance(item, dict)
        }
        critiques = []
        for claim in claims[:100]:
            claim_id = _services().clean_text(claim.get("id", ""), 80)
            verification_status = verification_by_id.get(claim_id, {}).get("status", "")
            scope = (
                "fulltext_page"
                if verification_status == "page_quote_matched"
                else (claim.get("evidence_scope") or "unknown")
            )
            text = _services().clean_text(claim.get("claim_text", ""), 1000)
            if scope == "abstract":
                critiques.append(
                    {
                        "claim_id": claim_id,
                        "category": "evidence_boundary",
                        "severity": "high",
                        "critique": "该 Claim 只有摘要证据，不能支持对完整实验、公式或局限的强结论。",
                    }
                )
            if claim.get("claim_type") == "result" and not _services().re.search(
                r"\d|improv|outperform|accuracy|f1|auc|结果", text, _services().re.I
            ):
                critiques.append(
                    {
                        "claim_id": claim_id,
                        "category": "unsupported_result",
                        "severity": "medium",
                        "critique": "被标记为结果 Claim，但摘录没有明确数值或比较对象，需要回到表格和实验设置核对。",
                    }
                )
            if claim.get("claim_type") == "method" and scope != "fulltext_page":
                critiques.append(
                    {
                        "claim_id": claim_id,
                        "category": "method_detail_missing",
                        "severity": "medium",
                        "critique": "方法 Claim 缺少全文页级证据，公式、超参数和实现细节尚未核验。",
                    }
                )
        if verification.get("evidence_not_found"):
            critiques.append(
                {
                    "claim_id": "",
                    "category": "evidence_integrity",
                    "severity": "high",
                    "critique": f"有 {verification['evidence_not_found']} 条证据摘录无法在保存的来源中重新定位。",
                }
            )
        now = _services().now_iso()
        with _services().DB_LOCK, _services().db_session() as db:
            for item in critiques:
                if not item.get("claim_id"):
                    continue
                db.execute(
                    "INSERT INTO claim_critiques(id,claim_id,goal_id,run_id,category,severity,critique_text,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        str(_services().uuid.uuid4()),
                        item["claim_id"],
                        _services().clean_text(goal.get("id", ""), 80),
                        run_id,
                        item["category"],
                        item["severity"],
                        item["critique"],
                        _services().json.dumps(
                            {
                                "scope": next(
                                    (
                                        c.get("evidence_scope")
                                        for c in claims
                                        if c.get("id") == item["claim_id"]
                                    ),
                                    "unknown",
                                )
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
        categories = {}
        for item in critiques:
            categories[item["category"]] = categories.get(item["category"], 0) + 1
        return {
            "critiques": critiques,
            "critique_count": len(critiques),
            "categories": categories,
            "open_issues": [
                item["critique"]
                for item in critiques
                if item["severity"] in {"high", "critical"}
            ],
            "status": "needs_review" if critiques else "no_obvious_issue",
        }
    if tool_name == "build_paper_analysis_overview":
        papers = (
            payload.get("papers") if isinstance(payload.get("papers"), list) else []
        )
        claims = (
            payload.get("claims") if isinstance(payload.get("claims"), list) else []
        )
        verification = (
            payload.get("verification")
            if isinstance(payload.get("verification"), dict)
            else {}
        )
        critique = (
            payload.get("critique") if isinstance(payload.get("critique"), dict) else {}
        )
        return _services()._paper_analysis_overview(
            papers, claims, verification, critique
        )
    if tool_name == "check_citation_completeness":
        previous = payload.get("previous") or {}
        claims = _services()._unique_agent_claims(
            previous.get("extract_claims", {}).get("claims", []),
            previous.get("resolve_comparison_gaps", {}).get("claims", []),
        )
        artifact = (
            previous.get("synthesize_map")
            or previous.get("draft_review")
            or previous.get("follow_up_map")
            or {}
        )
        # The synthesis artifact carries the original Claim IDs.  Comparing a
        # Claim ID with its paper ID made every non-empty review look incomplete
        # even when the source Claim had been preserved verbatim.
        cited = {
            item.get("id") or item.get("claim_id")
            for item in artifact.get("claims", [])
            if isinstance(item, dict)
        }
        missing = [item.get("id") for item in claims if item.get("id") not in cited]
        claim_groups = {}
        for item in artifact.get("claims", []):
            if not isinstance(item, dict):
                continue
            text = (
                _services()
                .re.sub(
                    r"\s+",
                    " ",
                    _services().clean_text(item.get("claim_text", ""), 2000),
                )
                .strip()
                .casefold()
            )
            if len(text) >= 24:
                claim_groups.setdefault(text, []).append(
                    item.get("id") or item.get("claim_id")
                )
        duplicate_claim_groups = [
            ids
            for ids in claim_groups.values()
            if len([value for value in ids if value]) > 1
        ]
        duplicate_claim_ids = [
            value for group in duplicate_claim_groups for value in group if value
        ][:100]
        return {
            "claims_total": len(claims),
            "claims_cited": len(claims) - len(missing),
            "missing_claim_ids": missing[:100],
            "duplicate_claim_groups": duplicate_claim_groups[:50],
            "duplicate_claim_ids": duplicate_claim_ids,
            "duplicate_claim_count": len(duplicate_claim_ids),
            "complete": not missing and not duplicate_claim_groups,
        }
    if tool_name == "evaluate_stop_conditions":
        previous = payload.get("previous") or {}
        coverage = previous.get("evaluate_coverage", {}).get("coverage", {})
        verification = (
            previous.get("verify_comparison_evidence")
            or previous.get("verify_evidence_refined")
            or previous.get("verify_evidence", {})
        )
        budget = payload.get("run", {}).get("budget", {})
        blocking_reasons = []
        if verification.get("needs_fulltext"):
            blocking_reasons.append("仍有摘要级证据需要全文核验")
        critique = (
            previous.get("critique_claims_final")
            or previous.get("critique_claims_refined")
            or previous.get("critique_claims", {})
        )
        if critique.get("open_issues"):
            blocking_reasons.append("存在高风险批判项")
        citation_check = previous.get("check_citation_completeness", {})
        if citation_check.get("duplicate_claim_count"):
            blocking_reasons.append("研究产物包含重复 Claim，需要合并后再停止")
        comparison_resolution = previous.get("resolve_comparison_gaps", {})
        if int(comparison_resolution.get("remaining_count") or 0) > 0:
            blocking_reasons.append(
                f"比较矩阵仍有 {int(comparison_resolution.get('remaining_count') or 0)} 个字段缺少页级证据"
            )
        if coverage.get("candidate_count", 0) < 5:
            blocking_reasons.append("候选数量尚未达到最低覆盖")
        stop = not blocking_reasons
        reasons = (
            ["覆盖、证据和批判检查均已达到当前预算"] if stop else list(blocking_reasons)
        )
        return {
            "stop": stop,
            "reasons": reasons,
            "blocking_reasons": blocking_reasons,
            "budget": budget,
        }
    if tool_name == "save_reading_card":
        paper_id = _services().clean_text(payload.get("paper_id", ""), 200)
        values = (
            payload.get("reading_card")
            if isinstance(payload.get("reading_card"), dict)
            else {}
        )
        if not paper_id:
            raise ValueError("需要 paper_id")
        with _services().DB_LOCK, _services().db_session() as db:
            if not db.execute(
                "SELECT 1 FROM papers WHERE id=?", (paper_id,)
            ).fetchone():
                raise ValueError("论文不存在或尚未导入文献库")
            current = _services().reading_card_dict(
                db.execute(
                    "SELECT * FROM reading_cards WHERE paper_id=?", (paper_id,)
                ).fetchone(),
                paper_id,
            )
            merged = _services().merge_ai_reading_card(
                current, values, bool(payload.get("merge_empty", True))
            )
            result = _services().save_reading_card(db, paper_id, merged, "agent")
            _services().record_audit(
                db,
                "agent.reading_card_applied",
                "paper",
                paper_id,
                {"run_id": payload.get("run_id", "")},
            )
        return {"paper_id": paper_id, "reading_card": result, "applied": True}
    if tool_name == "export_report":
        content = _services().clean_text(payload.get("content", ""), 200000)
        if not content:
            raise ValueError("没有可导出的研究报告内容")
        filename = (
            "research-report-"
            + _services().datetime.now().strftime("%Y%m%d-%H%M%S")
            + ".md"
        )
        return {
            "filename": filename,
            "content": content,
            "content_sha256": _services()
            .hashlib.sha256(content.encode("utf-8"))
            .hexdigest(),
            "created_at": _services().now_iso(),
        }
    raise ValueError(f"Agent 工具尚未实现：{tool_name}")


def _agent_update_step(run_id: str, step_id: str, **updates) -> None:
    allowed = {
        "status",
        "input_json",
        "output_json",
        "error",
        "attempts",
        "started_at",
        "updated_at",
        "completed_at",
        "progress_current",
        "progress_total",
        "message",
    }
    values = {key: value for key, value in updates.items() if key in allowed}
    values.setdefault("updated_at", _services().now_iso())
    if not values:
        return
    assignments = ",".join(f"{key}=?" for key in values)
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            f"UPDATE agent_steps SET {assignments} WHERE id=? AND run_id=?",
            (*values.values(), step_id, run_id),
        )


def _agent_update_run(run_id: str, **updates) -> None:
    allowed = {
        "status",
        "current_step_id",
        "error",
        "started_at",
        "updated_at",
        "completed_at",
    }
    values = {key: value for key, value in updates.items() if key in allowed}
    values.setdefault("updated_at", _services().now_iso())
    if not values:
        return
    assignments = ",".join(f"{key}=?" for key in values)
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            f"UPDATE agent_runs SET {assignments} WHERE id=?",
            (*values.values(), run_id),
        )


def _agent_schema_type_name(value: object) -> str:
    """Return a stable JSON-schema-like type name for diagnostics."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _agent_schema_type_matches(value: object, expected: str) -> bool:
    expected = str(expected or "").strip().casefold()
    if expected in {"", "any", "unknown"}:
        return True
    if expected in {"null", "none"}:
        return value is None
    if expected in {"object", "dict"}:
        return isinstance(value, dict)
    if expected in {"array", "list"}:
        return isinstance(value, list)
    if expected in {"string", "str"}:
        return isinstance(value, str)
    if expected in {"boolean", "bool"}:
        return isinstance(value, bool)
    if expected in {"integer", "int"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected in {"number", "float"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _agent_schema_path_value(value: object, path: str) -> object:
    """Read dotted and simple indexed paths used by completion rules."""
    if not path:
        return value
    current = value
    tokens = []
    for match in _services().re.finditer(r"([^.\[\]]+)|\[(\d+)\]", str(path)):
        tokens.append(match.group(1) if match.group(1) is not None else match.group(2))
    for key in tokens:
        if isinstance(current, dict) and isinstance(key, str):
            if key not in current:
                return _services()._AGENT_SCHEMA_MISSING
            current = current[key]
        elif isinstance(current, list):
            try:
                current = current[int(key)]
            except (TypeError, ValueError, IndexError):
                return _services()._AGENT_SCHEMA_MISSING
        else:
            return _services()._AGENT_SCHEMA_MISSING
    return current


def _agent_schema_errors(
    schema: object, value: object, path: str = "$", *, max_errors: int = 12
) -> list[str]:
    """Validate a compact JSON schema used by persisted Agent contracts.

    Existing contracts use the shorthand ``{"papers": "array"}``; newer
    contracts may use nested ``type/properties/items`` objects.  Optional
    fields are checked when present, while required fields are declared
    explicitly by a schema's ``required`` list or a step's completion rule.
    """
    errors: list[str] = []

    def add(message: str) -> None:
        if len(errors) < max_errors:
            errors.append(message)

    def visit(rule: object, current: object, current_path: str) -> None:
        if len(errors) >= max_errors or rule in (None, {}):
            return
        if isinstance(rule, str):
            if not _services()._agent_schema_type_matches(current, rule):
                add(
                    f"{current_path} 应为 {rule}，实际为 {_services()._agent_schema_type_name(current)}"
                )
            return
        if isinstance(rule, list):
            if not any(
                not _services()._agent_schema_errors(
                    item, current, current_path, max_errors=1
                )
                for item in rule
            ):
                add(f"{current_path} 不符合允许的结构")
            return
        if not isinstance(rule, dict):
            return
        if current is None and rule.get("nullable"):
            return
        expected = rule.get("type")
        if expected and not _services()._agent_schema_type_matches(current, expected):
            add(
                f"{current_path} 应为 {expected}，实际为 {_services()._agent_schema_type_name(current)}"
            )
            return
        enum = rule.get("enum") or rule.get("allowed_values")
        if isinstance(enum, list) and current not in enum:
            add(f"{current_path} 的值不在允许范围内：{current!r}")
        if isinstance(current, str):
            minimum = rule.get("min_length")
            maximum = rule.get("max_length")
            if minimum is not None and len(current) < int(minimum):
                add(f"{current_path} 长度 {len(current)} 小于 {minimum}")
            if maximum is not None and len(current) > int(maximum):
                add(f"{current_path} 长度 {len(current)} 超过 {maximum}")
        if isinstance(current, list):
            minimum = rule.get("min_items")
            maximum = rule.get("max_items")
            if minimum is not None and len(current) < int(minimum):
                add(f"{current_path} 数量 {len(current)} 小于 {minimum}")
            if maximum is not None and len(current) > int(maximum):
                add(f"{current_path} 数量 {len(current)} 超过 {maximum}")
            item_rule = rule.get("items")
            if item_rule is not None:
                for index, item in enumerate(current):
                    visit(item_rule, item, f"{current_path}[{index}]")
                    if len(errors) >= max_errors:
                        return
        if isinstance(current, dict):
            properties = rule.get("properties")
            if isinstance(properties, dict):
                required = rule.get("required") or []
                for field in required if isinstance(required, list) else []:
                    if (
                        _services()._agent_schema_path_value(current, str(field))
                        is _services()._AGENT_SCHEMA_MISSING
                    ):
                        add(f"{current_path}.{field} 为必填字段")
                for field, child_rule in properties.items():
                    if field in current:
                        visit(child_rule, current[field], f"{current_path}.{field}")

    # A flat mapping is the public shorthand used by the current contracts.
    if isinstance(schema, dict) and "type" not in schema and "properties" not in schema:
        if not isinstance(value, dict):
            return [
                f"{path} 应为 object，实际为 {_services()._agent_schema_type_name(value)}"
            ]
        for field, rule in schema.items():
            if field in {"required", "additional_properties"}:
                continue
            if field in value:
                visit(rule, value[field], f"{path}.{field}")
        required = schema.get("required")
        for field in required if isinstance(required, list) else []:
            if (
                _services()._agent_schema_path_value(value, str(field))
                is _services()._AGENT_SCHEMA_MISSING
            ):
                add(f"{path}.{field} 为必填字段")
        return errors
    visit(schema, value, path)
    return errors


def _agent_completion_path_required(result: dict, field: str) -> tuple[bool, object]:
    value = _services()._agent_schema_path_value(result, field)
    if (
        value is _services()._AGENT_SCHEMA_MISSING
        or value is None
        or value == ""
        or value == []
    ):
        return False, value
    return True, value


def _agent_record_validation_failure(
    run_id: str, step_id: str, detail: str, result: object = None
) -> None:
    """Turn a tool-call transport success into a visible contract failure."""
    with _services().DB_LOCK, _services().db_session() as db:
        call = db.execute(
            "SELECT id FROM agent_tool_calls WHERE run_id=? AND step_id=? ORDER BY started_at DESC LIMIT 1",
            (run_id, step_id),
        ).fetchone()
        if not call:
            return
        output = (
            _services()._bounded_agent_payload(result)
            if isinstance(result, dict)
            else None
        )
        db.execute(
            "UPDATE agent_tool_calls SET status='failed',error=?,output_json=COALESCE(?,output_json),completed_at=?,duration_ms=COALESCE(duration_ms,0) WHERE id=?",
            (
                _services().clean_text(detail, 1200),
                (
                    _services().json.dumps(output, ensure_ascii=False)
                    if output is not None
                    else None
                ),
                _services().now_iso(),
                call["id"],
            ),
        )


def _agent_execute_tool(
    run_id: str, step_id: str, tool_name: str, tool_class: str, payload: dict
) -> dict:
    started = _services().time.perf_counter()
    call_id = str(_services().uuid.uuid4())
    started_at = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "INSERT INTO agent_tool_calls(id,run_id,step_id,tool_name,tool_class,status,input_json,started_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                call_id,
                run_id,
                step_id,
                tool_name,
                tool_class,
                "running",
                _services().json.dumps(
                    _services()._bounded_agent_payload(payload, 80000),
                    ensure_ascii=False,
                ),
                started_at,
            ),
        )
    try:
        contract = _services().AGENT_TOOL_SPECS.get(tool_name, {})
        input_errors = _services()._agent_schema_errors(
            contract.get("input_schema") or {}, payload
        )
        if input_errors:
            raise ValueError("工具输入结构校验失败：" + "；".join(input_errors[:4]))
        if (
            contract.get("requires_confirmation") or tool_class in {"Apply", "External"}
        ) and payload.get("confirm") is not True:
            safe_local_preview = (
                tool_class not in {"Apply", "External"}
                and tool_name == "generate_research_brief"
            )
            if tool_name == "draft_reading_card":
                paper = (
                    payload.get("paper")
                    if isinstance(payload.get("paper"), dict)
                    else {}
                )
                paper_id = _services().clean_text(
                    paper.get("local_paper_id") or paper.get("id", ""), 200
                )
                with _services().DB_LOCK, _services().db_session() as db:
                    safe_local_preview = (
                        not paper_id
                        or paper_id.startswith("candidate:")
                        or not bool(
                            db.execute(
                                "SELECT 1 FROM attachments WHERE paper_id=? LIMIT 1",
                                (paper_id,),
                            ).fetchone()
                        )
                    )
            if not safe_local_preview:
                raise PermissionError(f"工具 {tool_name} 需要显式确认")
        # The DAG worker must remain responsive even when a connector or
        # compatible model endpoint stops responding.  Network functions have
        # their own bounded requests; this outer deadline also covers local
        # parsing and unexpected third-party code.
        with _services().AGENT_TOOL_EXECUTOR_LOCK:
            if _services().AGENT_TOOL_EXECUTOR is None:
                _services().AGENT_TOOL_EXECUTOR = _services().ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="research-agent-tool"
                )
            executor = _services().AGENT_TOOL_EXECUTOR
        timeout_seconds = max(
            1,
            int(
                _services()
                .AGENT_TOOL_SPECS.get(tool_name, {})
                .get("timeout_seconds", 120)
            ),
        )
        future = executor.submit(_services().call_agent_tool, tool_name, payload)
        try:
            result = future.result(timeout=timeout_seconds)
        except TimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                f"工具 {tool_name} 超过 {timeout_seconds} 秒执行预算"
            ) from exc
    except Exception as exc:
        detail = _services().clean_text(str(exc), 1200)
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "UPDATE agent_tool_calls SET status='failed',error=?,completed_at=?,duration_ms=? WHERE id=?",
                (
                    detail,
                    _services().now_iso(),
                    round((_services().time.perf_counter() - started) * 1000, 2),
                    call_id,
                ),
            )
        raise
    safe_result = _services()._bounded_agent_payload(result)
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "UPDATE agent_tool_calls SET status='success',output_json=?,completed_at=?,duration_ms=? WHERE id=?",
            (
                _services().json.dumps(safe_result, ensure_ascii=False),
                _services().now_iso(),
                round((_services().time.perf_counter() - started) * 1000, 2),
                call_id,
            ),
        )
    return result


def _agent_step_needs_confirmation(
    step: dict, payload: dict, current: dict, prior: dict
) -> bool:
    """Resolve confirmation at the point where a step can actually act.

    A plan may contain optional model drafting. Local deterministic drafting is
    safe to run automatically; page-aware full-text analysis and mutations are
    paused only when the required data/action is really available.
    """
    consented = (current.get("budget") or {}).get("consent_external") is True
    if step.get("tool_class") in {"Apply", "External"}:
        return not consented
    if not step.get("requires_confirmation"):
        return False
    if consented:
        return False
    tool_name = step.get("tool_name")
    if tool_name == "generate_research_brief":
        # The tool produces a local, metadata-only draft without consent. The
        # optional remote model branch checks the run budget itself.
        return False
    if tool_name == "draft_reading_card":
        paper = payload.get("paper") if isinstance(payload.get("paper"), dict) else {}
        paper_id = _services().clean_text(
            paper.get("local_paper_id") or paper.get("id", ""), 200
        )
        if not paper_id or paper_id.startswith("candidate:"):
            return False
        prepared = prior.get("prepare_fulltext", {}) if isinstance(prior, dict) else {}
        if isinstance(prepared, dict) and int(prepared.get("ready_count") or 0) <= 0:
            return False
        with _services().DB_LOCK, _services().db_session() as db:
            has_pdf = bool(
                db.execute(
                    "SELECT 1 FROM attachments WHERE paper_id=? LIMIT 1", (paper_id,)
                ).fetchone()
            )
        return has_pdf and bool(
            _services().model_settings().get("enabled") and _services().model_api_key()
        )
    return True


def _agent_completion_ok(step: dict, result: dict) -> tuple[bool, str]:
    if not isinstance(result, dict):
        return False, "工具没有返回 JSON 对象"
    schema_errors = _services()._agent_schema_errors(
        step.get("output_schema") or {}, result
    )
    if schema_errors:
        return False, "输出结构校验失败：" + "；".join(schema_errors[:4])
    completion = step.get("completion") or {}
    for field in completion.get("required", []):
        present, _value = _services()._agent_completion_path_required(
            result, str(field)
        )
        if not present:
            return False, f"缺少完成条件字段：{field}"
    allowed_values = completion.get("allowed_values") or {}
    for field, allowed in allowed_values.items():
        value = _services()._agent_schema_path_value(result, str(field))
        if (
            value is not _services()._AGENT_SCHEMA_MISSING
            and isinstance(allowed, list)
            and value not in allowed
        ):
            return False, f"Output field {field} has a disallowed status: {value}"
    # A queued or failed asynchronous task is not a completed Agent step.
    if result.get("status") in {
        "queued",
        "running",
        "failed",
        "cancelled",
        "interrupted",
    }:
        return False, f"Async task is not complete: {result.get('status')}"
    min_items = completion.get("min_items")
    if min_items is not None:
        candidates = (
            result.get("papers")
            or result.get("candidates")
            or result.get("claims")
            or result.get("groups")
            or []
        )
        if len(candidates) < int(min_items):
            return False, f"结果数量 {len(candidates)} 小于完成条件 {min_items}"
    for field, minimum in (completion.get("min_items_by_field") or {}).items():
        value = _services()._agent_schema_path_value(result, str(field))
        if (
            value is _services()._AGENT_SCHEMA_MISSING
            or not isinstance(value, list)
            or len(value) < int(minimum)
        ):
            count = len(value) if isinstance(value, list) else 0
            return False, f"字段 {field} 数量 {count} 小于完成条件 {minimum}"
    return True, ""


def _agent_restart_refinement_cycle(run_id: str, current: dict) -> bool:
    """Reset one finite evidence-refinement suffix of the DAG.

    The planner may discover that the first synthesis still has open evidence
    gaps. Resetting only the suffix after `resolve_evidence_gaps` reuses the
    expensive search/PDF work, preserves all prior outputs for audit, and
    cannot loop indefinitely because the counter is persisted in the run
    budget.
    """
    budget = dict(current.get("budget") or {})
    round_no = int(budget.get("refinement_round") or 0)
    maximum = min(2, max(0, int(budget.get("max_refinement_rounds", 1))))
    if round_no >= maximum:
        return False
    stop_step = next(
        (
            step
            for step in current.get("steps", [])
            if step.get("step_key") == "evaluate_stop_conditions"
        ),
        None,
    )
    stop_output = stop_step.get("output") if isinstance(stop_step, dict) else {}
    if not isinstance(stop_output, dict) or stop_output.get("stop") is not False:
        return False
    suffix_keys = {
        "resolve_evidence_gaps",
        "verify_evidence_refined",
        "critique_claims_refined",
        "compare_candidates",
        "resolve_comparison_gaps",
        "refresh_comparison",
        "verify_comparison_evidence",
        "critique_claims_final",
        "reading_route",
        "synthesize_map",
        "draft_review",
        "check_citation_completeness",
        "evaluate_stop_conditions",
    }
    reset = [
        step for step in current.get("steps", []) if step.get("step_key") in suffix_keys
    ]
    if not reset:
        return False
    now = _services().now_iso()
    budget["refinement_round"] = round_no + 1
    with _services().DB_LOCK, _services().db_session() as db:
        db.execute(
            "UPDATE agent_runs SET budget_json=?,status='running',error='',current_step_id='',updated_at=? WHERE id=?",
            (_services().json.dumps(budget, ensure_ascii=False), now, run_id),
        )
        for step in reset:
            message = f"受限证据收敛轮次 {round_no + 1}"
            db.execute(
                "UPDATE agent_steps SET status='queued',output_json='',input_json='',error='',message=?,attempts=0,started_at='',completed_at='',updated_at=? WHERE id=? AND status NOT IN ('cancelled', 'skipped')",
                (message, now, step["id"]),
            )
    return True


def _agent_build_step_payload(
    step: dict, current: dict, goal: dict, prior: dict, run_id: str
) -> dict:
    """Build one step's immutable input snapshot from completed DAG ancestors."""
    payload = {"goal": goal, "plan": current.get("plan", {}), "previous": prior}
    payload["run_id"] = run_id
    payload["step_id"] = step["id"]
    payload["confirm"] = bool((current.get("budget") or {}).get("consent_external"))
    key = step["step_key"]
    if key == "search_candidates":
        payload["query_plan"] = prior.get("interpret_goal", {})
    if key == "resolve_identity":
        payload["papers"] = prior.get("search_candidates", {}).get("papers", [])
    if key == "triage_candidates" or step["tool_name"] == "rank_candidates":
        payload["papers"] = prior.get("search_candidates", {}).get("papers", [])
        payload["local_context"] = prior.get("search_local_context", {})
    if key == "merge_versions":
        payload["papers"] = prior.get("search_candidates", {}).get("papers", [])
        payload["identity"] = prior.get("resolve_identity", {})
    if key == "search_local_context":
        payload["query"] = goal.get("core_question") or goal.get("objective", "")
        payload["limit"] = 20
    if key == "evaluate_coverage":
        payload["triage"] = prior.get("triage_candidates", {})
    if key == "expand_search":
        payload["coverage"] = prior.get("evaluate_coverage", {})
    if key == "expand_citations":
        payload["papers"] = list(
            prior.get("triage_candidates", {}).get("candidates", [])
        )
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], expanded, limit=50
            )
        payload["limit"] = 24
    if key == "extract_claims":
        payload["papers"] = prior.get("triage_candidates", {}).get("candidates", [])
        if prior.get("expand_search", {}).get("papers"):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], prior["expand_search"]["papers"], limit=50
            )
        if prior.get("expand_citations", {}).get("papers"):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], prior["expand_citations"]["papers"], limit=50
            )
    if key == "resolve_evidence_gaps":
        payload["claims"] = prior.get("extract_claims", {}).get("claims", [])
        payload["verification"] = prior.get("verify_evidence", {})
        payload["round"] = 1
    if key == "resolve_comparison_gaps":
        payload["papers"] = list(
            prior.get("triage_candidates", {}).get("candidates", [])
        )
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], expanded, limit=50
            )
        payload["claims"] = _services()._unique_agent_claims(
            prior.get("extract_claims", {}).get("claims", [])
        )
        payload["comparison"] = prior.get("refresh_comparison") or prior.get(
            "compare_candidates", {}
        )
    if key in {"critique_claims_refined", "critique_claims_final"}:
        payload["claims"] = prior.get("extract_claims", {}).get("claims", [])
        payload["verification"] = prior.get("verify_evidence_refined", {})
        if key == "critique_claims_final":
            payload["claims"] = _services()._unique_agent_claims(
                payload["claims"],
                prior.get("resolve_comparison_gaps", {}).get("claims", []),
            )
            payload["verification"] = prior.get("verify_comparison_evidence", {})
    if key == "paper_analysis_overview":
        payload["papers"] = list(
            prior.get("triage_candidates", {}).get("candidates", [])
        )
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], expanded, limit=8
            )
        cited = prior.get("expand_citations", {}).get("papers", [])
        if isinstance(cited, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], cited, limit=8
            )
        payload["claims"] = _services()._unique_agent_claims(
            prior.get("extract_claims", {}).get("claims", []),
            prior.get("resolve_comparison_gaps", {}).get("claims", []),
        )
        payload["verification"] = (
            prior.get("verify_comparison_evidence")
            or prior.get("verify_evidence_refined")
            or prior.get("verify_evidence", {})
        )
        payload["critique"] = (
            prior.get("critique_claims_final")
            or prior.get("critique_claims_refined")
            or prior.get("critique_claims", {})
        )
    if key == "extract_evidence":
        payload["paper_ids"] = [
            item.get("local_paper_id")
            for item in prior.get("triage_candidates", {}).get("candidates", [])
            if item.get("local_paper_id")
        ][:8]
    if key == "ocr_pages":
        payload["paper_ids"] = [
            item.get("local_paper_id")
            for item in prior.get("triage_candidates", {}).get("candidates", [])
            if item.get("local_paper_id")
        ][:8]
        payload["max_pages"] = 24
    if key == "prepare_fulltext":
        payload["papers"] = list(
            prior.get("triage_candidates", {}).get("candidates", [])
        )
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], expanded, limit=8
            )
        cited = prior.get("expand_citations", {}).get("papers", [])
        if isinstance(cited, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], cited, limit=8
            )
    if key in {"compare_candidates", "refresh_comparison", "reading_route"}:
        payload["papers"] = list(
            prior.get("triage_candidates", {}).get("candidates", [])
        )
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], expanded, limit=50
            )
        cited = prior.get("expand_citations", {}).get("papers", [])
        if isinstance(cited, list):
            payload["papers"] = _services()._unique_agent_papers(
                payload["papers"], cited, limit=50
            )
        payload["claims"] = _services()._unique_agent_claims(
            prior.get("extract_claims", {}).get("claims", []),
            prior.get("resolve_comparison_gaps", {}).get("claims", []),
        )
    if key == "draft_reading_card":
        candidates = list(prior.get("triage_candidates", {}).get("candidates", []))
        expanded = prior.get("expand_search", {}).get("papers", [])
        if isinstance(expanded, list):
            candidates = _services()._unique_agent_papers(
                candidates, expanded, limit=50
            )
        cited = prior.get("expand_citations", {}).get("papers", [])
        if isinstance(cited, list):
            candidates = _services()._unique_agent_papers(candidates, cited, limit=50)
        focused = _services()._agent_focus_papers(candidates, limit=1)
        payload["paper"] = focused[0] if focused else {}
    if key in {
        "synthesize_map",
        "draft_review",
        "follow_up_map",
        "evaluate_stop_conditions",
    }:
        payload["triage"] = prior.get("triage_candidates", {})
        payload["run"] = current
        payload["claims"] = _services()._unique_agent_claims(
            prior.get("extract_claims", {}).get("claims", []),
            prior.get("resolve_comparison_gaps", {}).get("claims", []),
        )
        payload["comparison"] = prior.get("compare_candidates", {})
        payload["reading_route"] = prior.get("reading_route", {})
        payload["reading_card_draft"] = prior.get("draft_reading_card", {})
        payload["follow_up"] = prior.get("follow_up_map", {})
        payload["verification"] = (
            prior.get("verify_comparison_evidence")
            or prior.get("verify_evidence_refined")
            or prior.get("verify_evidence", {})
        )
        payload["critique"] = (
            prior.get("critique_claims_final")
            or prior.get("critique_claims_refined")
            or prior.get("critique_claims", {})
        )
        payload["paper_analysis_overview"] = prior.get("paper_analysis_overview", {})
    return payload


def _agent_execute_step_attempt(
    run_id: str, step: dict, goal: dict, payload: dict, attempt: int
) -> None:
    try:
        result = _services()._agent_execute_tool(
            run_id, step["id"], step["tool_name"], step["tool_class"], payload
        )
        complete, completion_error = _services()._agent_completion_ok(step, result)
        if not complete:
            _services()._agent_record_validation_failure(
                run_id, step["id"], completion_error, result
            )
            raise RuntimeError(completion_error)
        output = _services()._bounded_agent_payload(result)
        _services()._agent_update_step(
            run_id,
            step["id"],
            status="success",
            output_json=_services().json.dumps(output, ensure_ascii=False),
            completed_at=_services().now_iso(),
        )
        if step["step_key"] in {
            "synthesize_map",
            "draft_review",
            "follow_up_map",
            "critique_claims",
            "critique_claims_refined",
            "critique_claims_final",
        }:
            artifact_id = str(_services().uuid.uuid4())
            with _services().DB_LOCK, _services().db_session() as db:
                artifact_type = result.get("artifact_type") or (
                    "critique"
                    if step["step_key"].startswith("critique_claims")
                    else "research_map"
                )
                version = db.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM agent_artifacts WHERE goal_id=? AND artifact_type=?",
                    (goal["id"], artifact_type),
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO agent_artifacts(id,goal_id,run_id,step_id,artifact_type,title,content_json,version,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        artifact_id,
                        goal["id"],
                        run_id,
                        step["id"],
                        artifact_type,
                        result.get("title", goal["name"]),
                        _services().json.dumps(output, ensure_ascii=False),
                        version,
                        _services().now_iso(),
                    ),
                )
    except Exception as exc:
        detail = _services().clean_text(str(exc), 1200)
        max_attempts = int(step.get("max_attempts") or 2)
        if attempt < max_attempts:
            _services()._agent_update_step(
                run_id,
                step["id"],
                status="queued",
                error=f"第 {attempt} 次失败，将重试：{detail}",
                completed_at="",
                message="等待自动重试",
            )
        else:
            _services()._agent_update_step(
                run_id,
                step["id"],
                status="failed",
                error=detail,
                completed_at=_services().now_iso(),
                message="已达到重试上限",
            )


def _agent_execute_ready_wave(
    run_id: str, current: dict, pending: list[dict], by_key: dict, max_calls: int
) -> str:
    stale_running = [item for item in pending if item.get("status") == "running"]
    if stale_running:
        detail = "检测到没有活动工作线程的运行中步骤，已停止以避免重复执行"
        with _services().DB_LOCK, _services().db_session() as db:
            now = _services().now_iso()
            for item in stale_running:
                db.execute(
                    "UPDATE agent_steps SET status='failed',error=?,message=?,updated_at=?,completed_at=? WHERE id=? AND status='running'",
                    (detail, "执行状态需要人工检查", now, now, item["id"]),
                )
        _services()._agent_update_run(
            run_id,
            status="partial",
            error=detail,
            completed_at=_services().now_iso(),
            current_step_id="",
        )
        return "done"

    ready = [
        item
        for item in pending
        if item.get("status") == "queued"
        and all(
            by_key.get(dep, {}).get("status") == "success"
            for dep in (item.get("dependencies") or [])
        )
    ]
    if not ready:
        blocked = [
            item
            for item in pending
            if item.get("status") == "queued"
            and any(
                by_key.get(dep, {}).get("status") in {"failed", "skipped", "cancelled"}
                for dep in (item.get("dependencies") or [])
            )
        ]
        if blocked:
            with _services().DB_LOCK, _services().db_session() as db:
                for item in blocked:
                    db.execute(
                        "UPDATE agent_steps SET status='skipped',error=?,updated_at=?,completed_at=? WHERE id=?",
                        (
                            "依赖步骤未完成，已跳过",
                            _services().now_iso(),
                            _services().now_iso(),
                            item["id"],
                        ),
                    )
            return "continue"
        awaiting = [
            item
            for item in current.get("steps", [])
            if item.get("status") == "awaiting_confirmation"
        ]
        if awaiting:
            _services()._agent_update_run(
                run_id,
                status="awaiting_confirmation",
                error="等待用户确认后继续",
                current_step_id=awaiting[0]["id"],
            )
            return "done"
        _services()._agent_update_run(
            run_id,
            status="partial",
            error="DAG 无可执行步骤，可能存在循环依赖或未满足的完成条件",
            completed_at=_services().now_iso(),
            current_step_id="",
        )
        return "done"

    with _services().DB_LOCK, _services().db_session() as db:
        goal_row = db.execute(
            "SELECT * FROM research_goals WHERE id=?", (current["goal"]["id"],)
        ).fetchone()
        goal = _services().research_goal_snapshot(goal_row)
        prior = {key: _services()._agent_step_output(db, run_id, key) for key in by_key}
        calls_done = int(
            db.execute(
                "SELECT COUNT(*) FROM agent_tool_calls WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
    available_calls = max_calls - calls_done
    if available_calls <= 0:
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "UPDATE agent_steps SET status='skipped',error=?,message=?,completed_at=?,updated_at=? WHERE run_id=? AND status='queued'",
                (
                    "达到工具调用预算",
                    "已达到运行预算",
                    _services().now_iso(),
                    _services().now_iso(),
                    run_id,
                ),
            )
        _services()._agent_update_run(
            run_id,
            status="partial",
            error=f"达到工具调用预算（{max_calls} 次）",
            completed_at=_services().now_iso(),
            current_step_id="",
        )
        return "done"

    worker_limit = min(_services().AGENT_STEP_CONCURRENCY, available_calls)
    work, awaiting = [], []
    for step in ready:
        payload = _services()._agent_build_step_payload(
            step, current, goal, prior, run_id
        )
        _services()._agent_update_step(
            run_id,
            step["id"],
            input_json=_services().json.dumps(
                _services()._bounded_agent_payload(payload, 80000), ensure_ascii=False
            ),
        )
        if _services()._agent_step_needs_confirmation(step, payload, current, prior):
            _services()._agent_update_step(
                run_id,
                step["id"],
                status="awaiting_confirmation",
                message="此步骤需要明确确认后才会访问外部模型或执行外部写入",
                error="",
                started_at="",
            )
            awaiting.append(step)
            continue
        if len(work) >= worker_limit:
            break
        attempt = int(step.get("attempts") or 0) + 1
        _services()._agent_update_step(
            run_id,
            step["id"],
            status="running",
            started_at=_services().now_iso(),
            attempts=attempt,
            error="",
            message="开始执行",
        )
        work.append((step, payload, attempt))

    if not work:
        existing_awaiting = [
            item
            for item in current.get("steps", [])
            if item.get("status") == "awaiting_confirmation"
        ]
        awaiting = awaiting or existing_awaiting
        if awaiting:
            _services()._agent_update_run(
                run_id,
                status="awaiting_confirmation",
                error="等待用户确认后继续",
                current_step_id=awaiting[0]["id"],
            )
            return "done"
        return "continue"

    _services()._agent_update_run(
        run_id, status="running", current_step_id=work[0][0]["id"], error=""
    )
    with _services().ThreadPoolExecutor(
        max_workers=len(work), thread_name_prefix="agent-dag"
    ) as pool:
        futures = [
            pool.submit(
                _services()._agent_execute_step_attempt,
                run_id,
                step,
                goal,
                payload,
                attempt,
            )
            for step, payload, attempt in work
        ]
        for future in _services().as_completed(futures):
            future.result()
    return "continue"


def run_agent_workflow(run_id: str) -> None:
    """Run ready DAG steps in bounded waves and persist every state change."""
    with _services().AGENT_ACTIVE_RUNS_LOCK:
        if run_id in _services().AGENT_ACTIVE_RUNS:
            return
        _services().AGENT_ACTIVE_RUNS.add(run_id)

    try:
        run = _services().agent_run_snapshot(run_id)
        if not run or run["status"] not in {
            "awaiting_confirmation",
            "queued",
            "running",
        }:
            return
        started_at = run.get("started_at") or _services().now_iso()
        started_perf = _services().time.perf_counter()
        budget = run.get("budget") or {}
        max_runtime = max(30, int(budget.get("max_runtime_seconds", 900)))
        max_calls = max(1, int(budget.get("max_tool_calls", 40)))
        _services()._agent_update_run(
            run_id, status="running", started_at=started_at, error=""
        )

        while True:
            current = _services().agent_run_snapshot(run_id)
            if not current:
                return
            status = current.get("status")
            if status in {"paused", "cancelled", "success", "partial", "failed"}:
                return
            if _services().time.perf_counter() - started_perf > max_runtime:
                with _services().DB_LOCK, _services().db_session() as db:
                    now = _services().now_iso()
                    db.execute(
                        "UPDATE agent_steps SET status='skipped',error=?,message=?,completed_at=?,updated_at=? WHERE run_id=? AND status='queued'",
                        ("达到总运行时间预算", "已达到运行预算", now, now, run_id),
                    )
                _services()._agent_update_run(
                    run_id,
                    status="partial",
                    error=f"达到总运行时间预算（{max_runtime} 秒）",
                    completed_at=_services().now_iso(),
                    current_step_id="",
                )
                return

            by_key = {item["step_key"]: item for item in current.get("steps", [])}
            pending = [
                item
                for item in current.get("steps", [])
                if item.get("status") in {"queued", "running"}
            ]
            if pending:
                outcome = _services()._agent_execute_ready_wave(
                    run_id, current, pending, by_key, max_calls
                )
                if outcome == "done":
                    return
                continue

            awaiting = [
                item
                for item in current.get("steps", [])
                if item.get("status") == "awaiting_confirmation"
            ]
            if awaiting:
                _services()._agent_update_run(
                    run_id,
                    status="awaiting_confirmation",
                    error="等待用户确认后继续",
                    current_step_id=awaiting[0]["id"],
                )
                return

            failed = [
                item
                for item in current.get("steps", [])
                if item.get("status") == "failed"
            ]
            if failed:
                _services()._agent_update_run(
                    run_id,
                    status="partial",
                    error=f"有 {len(failed)} 个步骤失败，结果仅部分完成",
                    completed_at=_services().now_iso(),
                    current_step_id="",
                )
                return

            if _services()._agent_restart_refinement_cycle(run_id, current):
                continue

            stop_step = next(
                (
                    item
                    for item in current.get("steps", [])
                    if item.get("step_key") == "evaluate_stop_conditions"
                ),
                None,
            )
            stop_output = stop_step.get("output") if isinstance(stop_step, dict) else {}
            unresolved = (
                stop_output.get("blocking_reasons")
                if isinstance(stop_output, dict)
                else []
            )
            if (
                isinstance(stop_output, dict)
                and stop_output.get("stop") is False
                and unresolved
            ):
                message = "停止条件仍有未解决项：" + "；".join(
                    str(item) for item in unresolved[:3]
                )
                budget = dict(current.get("budget") or {})
                budget["completion_state"] = "partial"
                with _services().DB_LOCK, _services().db_session() as db:
                    db.execute(
                        "UPDATE agent_runs SET budget_json=?,error=?,updated_at=? WHERE id=?",
                        (
                            _services().json.dumps(budget, ensure_ascii=False),
                            message,
                            _services().now_iso(),
                            run_id,
                        ),
                    )

            _services()._agent_update_run(
                run_id,
                status="success",
                current_step_id="",
                completed_at=_services().now_iso(),
            )
            with _services().DB_LOCK, _services().db_session() as db:
                goal_id = db.execute(
                    "SELECT goal_id FROM agent_runs WHERE id=?", (run_id,)
                ).fetchone()[0]
                db.execute(
                    "UPDATE research_goals SET current_stage='artifact_ready',updated_at=? WHERE id=?",
                    (_services().now_iso(), goal_id),
                )
                _services().record_audit(
                    db, "agent.run_completed", "agent_run", run_id, {"goal_id": goal_id}
                )
            return
    except Exception as exc:
        current = _services().agent_run_snapshot(run_id, include_steps=False)
        if current and current.get("status") not in {
            "paused",
            "cancelled",
            "success",
            "partial",
            "failed",
        }:
            _services()._agent_update_run(
                run_id,
                status="failed",
                error=_services().clean_text(str(exc), 1200),
                completed_at=_services().now_iso(),
                current_step_id="",
            )
    finally:
        with _services().AGENT_ACTIVE_RUNS_LOCK:
            _services().AGENT_ACTIVE_RUNS.discard(run_id)


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
                "SELECT id,status,error FROM agent_runs WHERE id=?", (run_id,)
            ).fetchone()
            if (
                not row
                or row["status"] != "interrupted"
                or row["error"] != "服务重启时任务未完成"
            ):
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
    "_agent_step_output",
    "_bounded_agent_payload",
    "_agent_reduced_paper",
    "_agent_resolve_paper_identities",
    "_agent_focus_papers",
    "_infer_page_section",
    "_agent_paper_key",
    "_unique_agent_papers",
    "_unique_agent_claims",
    "_record_goal_paper_history",
    "_agent_fulltext_reading_card",
    "_paper_analysis_overview",
    "call_agent_tool",
    "_agent_update_step",
    "_agent_update_run",
    "_agent_schema_type_name",
    "_agent_schema_type_matches",
    "_agent_schema_path_value",
    "_agent_schema_errors",
    "_agent_completion_path_required",
    "_agent_record_validation_failure",
    "_agent_execute_tool",
    "_agent_step_needs_confirmation",
    "_agent_completion_ok",
    "_agent_restart_refinement_cycle",
    "_agent_build_step_payload",
    "_agent_execute_step_attempt",
    "_agent_execute_ready_wave",
    "run_agent_workflow",
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
