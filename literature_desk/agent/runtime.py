"""Autonomous LangChain/LangGraph Agent runtime.

This layer deliberately delegates domain work to the existing audited tool
contracts.  The framework decides which read/draft tool to call next; it does
not receive direct database or filesystem access.
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from ..services import app_services as _services
from .persistence import (
    add_artifact,
    completed_tool_call,
    create_run,
    finish_run,
    mark_step,
    start_step,
)
from .tools import build_langchain_tools, tool_catalog
from .memory import retrieve_goal_memory
from .decision import _model_action

try:
    from langgraph.graph import END, StateGraph
except ImportError:  # pragma: no cover - optional dependency
    END = None
    StateGraph = None


class AgentState(TypedDict, total=False):
    objective: str
    context: dict[str, Any]
    max_steps: int
    confirm: bool
    steps: int
    history: list[dict[str, Any]]
    results: list[dict[str, Any]]
    action: dict[str, Any]
    answer: str
    done: bool
    needs_confirmation: bool
    error: str
    run_id: str
    goal_id: str
    step_ordinal: int
    memory: dict[str, Any]


def agent_framework_status() -> dict[str, Any]:
    """Expose framework availability for diagnostics and the settings UI."""
    available_tools = build_langchain_tools()
    return {
        "langchain": bool(available_tools),
        "langgraph": StateGraph is not None,
        "runtime": "langgraph" if StateGraph is not None else "unavailable",
        "autonomous_tool_selection": StateGraph is not None and bool(available_tools),
        "model_native_tool_calls": True,
        "decision_modes": ["call_tool", "reflect", "finish"],
        "memory": True,
        "graph_nodes": ["decide", "execute", "reflect"],
        "tool_count": len(tool_catalog()),
    }


def _safe_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    # Context is prompt material, never an instruction channel. Bound it so
    # a pasted document cannot consume the entire planning request.
    def bound(item: Any, depth: int = 0) -> Any:
        if depth >= 4:
            return _services().clean_text(item, 1000) if item is not None else None
        if isinstance(item, str):
            return item[:4000]
        if isinstance(item, dict):
            return {
                str(key)[:80]: bound(child, depth + 1)
                for key, child in list(item.items())[:30]
            }
        if isinstance(item, (list, tuple)):
            return [bound(child, depth + 1) for child in list(item)[:50]]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return _services().clean_text(item, 1000)

    return {
        str(key)[:80]: bound(item)
        for key, item in list(value.items())[:30]
    }


def _memory_goal_id(data: dict[str, Any], context: dict[str, Any]) -> str:
    """Resolve the durable memory boundary without trusting prompt text.

    ``goal_id`` is a transport field, not an instruction.  Only a bounded
    identifier is used for the database lookup; arbitrary context remains
    prompt material and cannot select another memory source.
    """
    value = data.get("goal_id") or context.get("goal_id")
    return _services().clean_text(value, 160) if value else ""


def _initial_memory(data: dict[str, Any], context: dict[str, Any], objective: str) -> dict[str, Any]:
    goal_id = _memory_goal_id(data, context)
    if not goal_id:
        return {"papers": [], "artifacts": [], "feedback": []}
    try:
        return retrieve_goal_memory(goal_id, objective)
    except Exception:
        # Memory is an enhancement to the controller.  A stale or partially
        # migrated record must not prevent a new run from answering.
        return {"papers": [], "artifacts": [], "feedback": []}


def _tool_payload(tool_name: str, objective: str, context: dict[str, Any], args: Any) -> dict[str, Any]:
    payload = dict(args) if isinstance(args, dict) else {}
    goal = payload.get("goal")
    if not isinstance(goal, dict):
        goal = {}
    if context:
        payload.setdefault("context", context)
        goal.setdefault("id", context.get("goal_id", ""))
        for key in ("sources", "focus", "exclusions", "deliverables", "time_range"):
            if context.get(key):
                goal.setdefault(key, context[key])
    if tool_name in {"build_search_plan", "search_sources"}:
        goal.setdefault("objective", objective)
        goal.setdefault("core_question", objective)
        goal.setdefault("sources", ["arxiv", "openalex", "crossref"])
    if goal:
        payload["goal"] = goal
    return payload


def _normalize_action(action: Any, catalog: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(action, dict):
        return {"action": "finish", "answer": "模型没有返回可执行的决策。"}
    if str(action.get("action", "")).casefold() in {"finish", "done", "stop"}:
        return {"action": "finish", "answer": str(action.get("answer") or "已完成当前 Agent 任务")[:4000]}
    if str(action.get("action", "")).casefold() == "reflect":
        return {"action": "reflect", "answer": str(action.get("answer") or "正在复核当前证据")[:2000]}
    allowed = {item["name"]: item for item in catalog}
    tool_name = str(action.get("tool", "")).strip()
    if tool_name not in allowed:
        return {"action": "finish", "answer": f"模型选择了不可用工具：{tool_name or '空'}"}
    return {
        "action": "call_tool",
        "tool": tool_name,
        "arguments": action.get("arguments") if isinstance(action.get("arguments"), dict) else {},
    }


def _execute_action(state: AgentState, action: dict[str, Any], tools: dict[str, Any], catalog_by_name: dict[str, dict[str, Any]]) -> AgentState:
    if action.get("action") == "finish":
        return {**state, "done": True, "answer": action.get("answer", "")}
    if action.get("action") == "reflect":
        reflection = str(action.get("answer", ""))[:2000]
        note = {"tool": "reflection", "arguments": {}, "result": {"reflection": reflection}}
        previous_steps = int(state.get("steps", 0))
        next_steps = previous_steps + 1
        run_id = state.get("run_id", "")
        if run_id:
            step_id = start_step(
                run_id,
                int(state.get("step_ordinal", 0)),
                "reflection",
                "Draft",
                {"objective": state.get("objective", "")},
            )
            mark_step(
                run_id,
                step_id,
                status="success",
                output={"reflection": reflection},
                message="完成证据缺口复核",
            )
            add_artifact(
                run_id,
                state.get("goal_id", ""),
                step_id,
                "agent_reflection",
                {"objective": state.get("objective", ""), "reflection": reflection},
                "Agent 证据复核",
            )
        # A reflection requested after the tool budget is already exhausted is
        # itself the final answer.  Reflections before exhaustion get another
        # decision turn so the model can act on the identified gap.
        exhausted_before_turn = previous_steps >= int(state.get("max_steps", 0))
        return {
            **state,
            "history": [*state.get("history", []), note][-12:],
            "steps": next_steps,
            "step_ordinal": int(state.get("step_ordinal", 0)) + 1,
            "done": exhausted_before_turn,
            "answer": action.get("answer", "") if exhausted_before_turn else state.get("answer", ""),
        }
    name = action["tool"]
    spec = catalog_by_name[name]
    payload = _tool_payload(name, state.get("objective", ""), state.get("context", {}), action.get("arguments"))
    if state.get("confirm") is True:
        payload.setdefault("confirm", True)
    if spec.get("requires_confirmation") and not state.get("confirm"):
        run_id = state.get("run_id", "")
        if run_id:
            step_id = str(action.get("_existing_step_id") or "") or start_step(
                run_id,
                int(state.get("step_ordinal", 0)),
                name,
                str(spec.get("class", "Read")),
                payload,
                True,
            )
            mark_step(
                run_id,
                step_id,
                status="awaiting_confirmation",
                message="等待用户确认后才能调用外部或写入工具",
            )
        return {
            **state,
            "done": True,
            "needs_confirmation": True,
            "answer": f"下一步需要确认后才能调用工具：{name}",
        }
    run_id = state.get("run_id", "")
    goal_id = state.get("goal_id", "")
    step_id = str(action.get("_existing_step_id") or "")
    if run_id:
        if not step_id:
            step_id = start_step(
                run_id,
                int(state.get("step_ordinal", 0)),
                name,
                str(spec.get("class", "Read")),
                payload,
                bool(spec.get("requires_confirmation")),
            )
    tool = tools.get(name)
    if tool is None:
        result: Any = {"error": "LangChain 工具不可用"}
    else:
        try:
            invocation = {"payload_json": json.dumps(payload, ensure_ascii=False)}
            config = {
                "metadata": {
                    "litloom_run_id": run_id,
                    "litloom_step_id": step_id,
                    "litloom_tool_class": str(spec.get("class", "Read")),
                }
            } if run_id else None
            result = completed_tool_call(run_id, step_id) if run_id and step_id else None
            if result is None:
                raw_result = tool.invoke(invocation, config=config)
                result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        except Exception as exc:
            result = {"error": str(exc)[:1000]}
            if run_id:
                mark_step(run_id, step_id, status="failed", output=result, error=str(exc), message="工具执行失败")
        else:
            if run_id:
                mark_step(run_id, step_id, status="success", output=result, message="工具执行完成")
                add_artifact(run_id, goal_id, step_id, "autonomous_tool_result", result, f"工具结果 · {name}")
    history = [*state.get("history", []), {"tool": name, "arguments": payload, "result": _services()._bounded_agent_payload(result, 24000)}]
    results = [*state.get("results", []), {"tool": name, "result": result}]
    return {**state, "steps": int(state.get("steps", 0)) + 1, "step_ordinal": int(state.get("step_ordinal", 0)) + 1, "history": history[-12:], "results": results[-12:]}


def _build_langgraph(tools: dict[str, Any], catalog: list[dict[str, Any]]):
    if StateGraph is None or END is None:
        return None
    by_name = {item["name"]: item for item in catalog}

    def decide(state: AgentState) -> AgentState:
        if state.get("done"):
            return state
        action = _normalize_action(
            _model_action(
                state,
                catalog,
                allow_tools=int(state.get("steps", 0)) < int(state.get("max_steps", 6)),
            ),
            catalog,
        )
        return {**state, "action": action}

    def execute(state: AgentState) -> AgentState:
        return _execute_action(state, state.get("action") or {"action": "finish"}, tools, by_name)

    def reflect(state: AgentState) -> AgentState:
        return _execute_action(
            state,
            state.get("action") or {"action": "reflect", "answer": "正在复核当前证据"},
            tools,
            by_name,
        )

    def route(state: AgentState) -> str:
        if state.get("done"):
            return END
        if (state.get("action") or {}).get("action") == "reflect":
            return "reflect"
        return "execute"

    graph = StateGraph(AgentState)
    graph.add_node("decide", decide)
    graph.add_node("execute", execute)
    graph.add_node("reflect", reflect)
    graph.set_entry_point("decide")
    graph.add_conditional_edges(
        "decide",
        route,
        {"execute": "execute", "reflect": "reflect", END: END},
    )
    graph.add_edge("execute", "decide")
    graph.add_edge("reflect", "decide")
    return graph.compile()


def _persist_autonomous_result(
    run_id: str, goal_id: str, objective: str, final: AgentState
) -> None:
    with _services().DB_LOCK, _services().db_session() as db:
        row = db.execute(
            "SELECT id FROM agent_steps WHERE run_id=? ORDER BY ordinal DESC,id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    step_id = row["id"] if row else start_step(
        run_id,
        0,
        "model_synthesis",
        "Draft",
        {"objective": objective},
    )
    if not row:
        mark_step(
            run_id,
            step_id,
            status="success",
            output={"answer": final.get("answer", "")},
            message="模型完成最终综合",
        )
    add_artifact(
        run_id,
        goal_id,
        step_id,
        "agent_autonomous_result",
        {
            "objective": objective,
            "answer": final.get("answer", ""),
            "results": final.get("results", []),
        },
        "自主 Agent 研究结果",
    )


def autonomous_agent(data: dict[str, Any]) -> dict[str, Any]:
    """Run a bounded tool-using Agent and return an auditable execution trace."""
    if not isinstance(data, dict):
        raise ValueError("Agent 请求必须是 JSON 对象")
    objective = _services().clean_text(data.get("objective", ""), 12000)
    if len(objective) < 8:
        raise ValueError("研究目标至少需要 8 个字符")
    settings = _services().model_settings()
    if not settings.get("enabled") or not _services().model_api_key():
        raise ValueError("自主 Agent 需要先启用并配置模型 API")
    max_steps = max(1, min(int(data.get("max_steps", 6) or 6), 12))
    confirm = data.get("confirm") is True
    context = _safe_context(data.get("context"))
    if data.get("goal_id") and "goal_id" not in context:
        context["goal_id"] = _services().clean_text(data.get("goal_id"), 160)
    catalog = tool_catalog()
    tools = build_langchain_tools()
    if not tools:
        raise RuntimeError("LangChain/LangGraph 工具依赖未安装，请运行 python -m pip install -e .")
    framework = "langgraph" if StateGraph is not None else "langchain-local"
    memory = _initial_memory(data, context, objective)
    persisted = create_run(
        objective,
        context,
        max_steps,
        catalog,
        framework,
        _memory_goal_id(data, context),
    )
    initial: AgentState = {
        "objective": objective,
        "context": context,
        "memory": memory,
        "max_steps": max_steps,
        "confirm": confirm,
        "steps": 0,
        "history": [],
        "results": [],
        "done": False,
        "run_id": persisted["run_id"],
        "goal_id": persisted["goal_id"],
        "step_ordinal": 0,
    }
    graph = _build_langgraph(tools, catalog)
    if graph is None:
        finish_run(
            persisted["run_id"],
            status="failed",
            error="LangGraph 未安装；自主 Agent 需要 LangGraph 运行时支持",
            answer="自主 Agent 不可用",
        )
        raise RuntimeError("LangGraph 未安装；请重新安装项目依赖")
    try:
        final = graph.invoke(initial, config={"recursion_limit": max_steps * 2 + 4})
        status = "needs_confirmation" if final.get("needs_confirmation") else "success"
        if status == "success":
            _persist_autonomous_result(
                persisted["run_id"],
                persisted["goal_id"],
                objective,
                final,
            )
        finish_run(persisted["run_id"], status=status, answer=final.get("answer", ""), results=final.get("results", []))
        return {
            "run_id": persisted["run_id"],
            "goal_id": persisted["goal_id"],
            "status": status,
            "framework": framework,
            "objective": objective,
            "answer": final.get("answer", ""),
            "steps": final.get("steps", 0),
            "results": final.get("results", []),
            "history": final.get("history", []),
            "tool_catalog": catalog,
        }
    except Exception as exc:
        finish_run(persisted["run_id"], status="failed", error=str(exc), answer="自主 Agent 执行失败")
        raise


def execute_autonomous_run(run_id: str) -> dict[str, Any]:
    """Resume an autonomous run from its persisted tool observations."""
    run = _services().agent_run_snapshot(run_id)
    if not run or run.get("workflow") != "autonomous":
        raise LookupError("自主 Agent 运行不存在")
    goal = run.get("goal") or {}
    objective = _services().clean_text(goal.get("objective", ""), 12000)
    budget = run.get("budget") or {}
    max_steps = max(1, min(int(budget.get("max_steps", 8) or 8), 12))
    goal_context = {
        "goal_id": goal.get("id", ""),
        "sources": goal.get("sources", []),
        "focus": goal.get("focus", []),
        "exclusions": goal.get("exclusions", []),
        "deliverables": goal.get("deliverables", []),
        "time_range": goal.get("time_range", {}),
    }
    plan = run.get("plan") or {}
    catalog = (
        plan.get("tool_catalog")
        or plan.get("tools")
        or plan.get("available_tools")
        or tool_catalog()
    )
    tools = build_langchain_tools()
    graph = _build_langgraph(tools, catalog)
    if graph is None:
        raise RuntimeError("LangGraph 未安装；自主 Agent 需要 LangGraph 运行时支持")

    history = []
    completed_steps = []
    for step in run.get("steps", []):
        if step.get("status") not in {"success", "failed"}:
            continue
        result = step.get("output") or {"error": step.get("error", "工具执行失败")}
        history.append({
            "tool": step.get("tool_name", ""),
            "arguments": step.get("input", {}),
            "result": _services()._bounded_agent_payload(result, 24000),
        })
        completed_steps.append(step)
    initial: AgentState = {
        "objective": objective,
        "context": _safe_context(goal_context),
        "memory": retrieve_goal_memory(goal.get("id", ""), objective),
        "max_steps": max_steps,
        "confirm": budget.get("consent_external") is True,
        "steps": len(history),
        "history": history[-12:],
        "results": [{"tool": item["tool"], "result": item["result"]} for item in history[-12:]],
        "done": False,
        "run_id": run_id,
        "goal_id": goal.get("id", ""),
        "step_ordinal": len(history),
    }
    by_name = {item["name"]: item for item in catalog}
    pending = next(
        (
            step for step in run.get("steps", [])
            if step.get("step_key", "").startswith("autonomous_")
            and step.get("status") == "queued"
        ),
        None,
    )
    if pending:
        initial = _execute_action(
            initial,
            {
                "action": "call_tool",
                "tool": pending["tool_name"],
                "arguments": pending.get("input") or {},
                "_existing_step_id": pending["id"],
            },
            tools,
            by_name,
        )
    final = graph.invoke(initial, config={"recursion_limit": max_steps * 2 + 4})
    status = "needs_confirmation" if final.get("needs_confirmation") else "success"
    if status == "success":
        _persist_autonomous_result(
            run_id,
            goal.get("id", ""),
            objective,
            final,
        )
        with _services().DB_LOCK, _services().db_session() as db:
            db.execute(
                "UPDATE research_goals SET current_stage='artifact_ready',updated_at=? WHERE id=?",
                (_services().now_iso(), goal.get("id", "")),
            )
    finish_run(run_id, status=status, answer=final.get("answer", ""), results=final.get("results", []))
    return {
        "run_id": run_id,
        "goal_id": goal.get("id", ""),
        "status": status,
        "framework": "langgraph",
        "objective": objective,
        "answer": final.get("answer", ""),
        "steps": final.get("steps", 0),
        "results": final.get("results", []),
        "history": final.get("history", []),
        "tool_catalog": catalog,
    }


__all__ = ["AgentState", "agent_framework_status", "autonomous_agent", "execute_autonomous_run"]
