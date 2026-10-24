"""LangChain tool adapters over LitLoom's audited domain tools."""

import json
from typing import Any

from ..services import app_services as _services

try:
    from langchain_core.tools import StructuredTool
    from langchain_core.runnables import RunnableConfig
except ImportError:  # pragma: no cover - optional dependency
    StructuredTool = None
    RunnableConfig = Any


# The autonomous controller sees the same audited tool contracts as the
# specialist workflow runner.  The model still chooses at most one action per
# turn, while the execution boundary enforces schemas and confirmation.  Keep
# this list in one place so adding a domain tool does not silently make it
# unavailable to the autonomous Agent.
DEFAULT_AGENT_TOOLS = (
    "build_search_plan",
    "search_sources",
    "browse_web",
    "fetch_web_page",
    "search_local_library",
    "resolve_paper_identity",
    "merge_paper_versions",
    "rank_candidates",
    "evaluate_coverage",
    "expand_search",
    "prepare_fulltext",
    "download_fulltext",
    "extract_pdf_pages",
    "ocr_pdf_pages",
    "render_pdf_page",
    "search_document",
    "expand_citations",
    "extract_claims",
    "verify_evidence",
    "resolve_evidence_gaps",
    "critique_claims",
    "build_paper_analysis_overview",
    "check_citation_completeness",
    "evaluate_stop_conditions",
    "compare_papers",
    "resolve_comparison_gaps",
    "generate_reading_route",
    "generate_research_brief",
    "track_follow_up",
    "draft_reading_card",
    "save_reading_card",
    "export_report",
)


def _tool_description(name: str, spec: dict[str, Any]) -> str:
    description = str(spec.get("description") or f"调用 LitLoom 的 {name} 工具")
    input_schema = spec.get("input_schema") or {}
    return (
        f"{description}。输入 payload_json 必须是 JSON 对象字符串，字段约束："
        f"{json.dumps(input_schema, ensure_ascii=False, separators=(',', ':'))}。"
        "工具只返回来源可追溯的结构化结果。"
    )


def available_tool_specs(names: tuple[str, ...] = DEFAULT_AGENT_TOOLS) -> dict[str, dict]:
    specs = _services().AGENT_TOOL_SPECS
    return {
        name: dict(specs[name])
        for name in names
        if name in specs and specs[name].get("available")
    }


def build_langchain_tools(
    names: tuple[str, ...] = DEFAULT_AGENT_TOOLS,
) -> dict[str, Any]:
    """Build one LangChain StructuredTool per existing audited tool."""
    if StructuredTool is None:
        return {}
    tools: dict[str, Any] = {}
    for name, spec in available_tool_specs(names).items():
        def make_invoke(tool_name: str):
            def invoke(
                payload_json: str, config: RunnableConfig
            ) -> str:
                """Run the validated LitLoom tool through LangChain."""
                try:
                    payload = json.loads(payload_json or "{}")
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError("payload_json 必须是有效 JSON 对象") from exc
                if not isinstance(payload, dict):
                    raise ValueError("payload_json 必须解码为 JSON 对象")
                metadata = (config or {}).get("metadata") or {}
                run_id = str(metadata.get("litloom_run_id") or "")
                step_id = str(metadata.get("litloom_step_id") or "")
                if run_id and step_id:
                    result = _services()._agent_execute_tool(
                        run_id,
                        step_id,
                        tool_name,
                        str(metadata.get("litloom_tool_class") or "Read"),
                        payload,
                    )
                else:
                    result = _services().call_agent_tool(tool_name, payload)
                return json.dumps(result, ensure_ascii=False)

            return invoke

        invoke = make_invoke(name)

        tools[name] = StructuredTool.from_function(
            func=invoke,
            name=name,
            description=_tool_description(name, spec),
        )
    return tools


def tool_catalog(names: tuple[str, ...] = DEFAULT_AGENT_TOOLS) -> list[dict[str, Any]]:
    """Return a prompt-safe catalog without secrets or implementation details."""
    return [
        {
            "name": name,
            "class": spec.get("class", "Read"),
            "description": spec.get("description", ""),
            "requires_confirmation": bool(spec.get("requires_confirmation")),
            "timeout_seconds": spec.get("timeout_seconds", 120),
            "input_schema": spec.get("input_schema", {}),
        }
        for name, spec in available_tool_specs(names).items()
    ]


__all__ = [
    "DEFAULT_AGENT_TOOLS",
    "available_tool_specs",
    "build_langchain_tools",
    "tool_catalog",
]
