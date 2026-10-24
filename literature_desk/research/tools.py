"""Agent tool validation, consent checks, and dispatch.

Individual tool implementations live in three responsibility-oriented modules;
this facade preserves the public ``call_agent_tool`` API.
"""

from __future__ import annotations

from ..services import app_services as _services

from .tools_search import (
    _tool_build_search_plan,
    _tool_search_sources,
    _tool_browse_web,
    _tool_fetch_web_page,
    _tool_search_local_library,
    _tool_resolve_paper_identity,
    _tool_merge_paper_versions,
    _tool_rank_candidates,
    _tool_evaluate_coverage,
    _tool_expand_search,
)
from .tools_documents import (
    _tool_prepare_fulltext,
    _tool_download_fulltext,
    _tool_extract_pdf_pages,
    _tool_ocr_pdf_pages,
    _tool_render_pdf_page,
    _tool_search_document,
    _tool_expand_citations,
)
from .tools_workflows import (
    _tool_track_follow_up,
    _tool_generate_reading_route,
    _tool_compare_papers,
    _tool_resolve_comparison_gaps,
    _tool_generate_research_brief,
)
from .tools_analysis import (
    _tool_draft_reading_card,
    _tool_extract_claims,
    _tool_resolve_evidence_gaps,
    _tool_verify_evidence,
    _tool_critique_claims,
    _tool_build_paper_analysis_overview,
    _tool_check_citation_completeness,
    _tool_evaluate_stop_conditions,
    _tool_save_reading_card,
    _tool_export_report,
)

_TOOL_HANDLERS = {
    "build_search_plan": _tool_build_search_plan,
    "search_sources": _tool_search_sources,
    "browse_web": _tool_browse_web,
    "fetch_web_page": _tool_fetch_web_page,
    "search_local_library": _tool_search_local_library,
    "resolve_paper_identity": _tool_resolve_paper_identity,
    "merge_paper_versions": _tool_merge_paper_versions,
    "rank_candidates": _tool_rank_candidates,
    "evaluate_coverage": _tool_evaluate_coverage,
    "expand_search": _tool_expand_search,
    "prepare_fulltext": _tool_prepare_fulltext,
    "download_fulltext": _tool_download_fulltext,
    "extract_pdf_pages": _tool_extract_pdf_pages,
    "ocr_pdf_pages": _tool_ocr_pdf_pages,
    "render_pdf_page": _tool_render_pdf_page,
    "search_document": _tool_search_document,
    "expand_citations": _tool_expand_citations,
    "track_follow_up": _tool_track_follow_up,
    "generate_reading_route": _tool_generate_reading_route,
    "compare_papers": _tool_compare_papers,
    "resolve_comparison_gaps": _tool_resolve_comparison_gaps,
    "generate_research_brief": _tool_generate_research_brief,
    "draft_reading_card": _tool_draft_reading_card,
    "extract_claims": _tool_extract_claims,
    "resolve_evidence_gaps": _tool_resolve_evidence_gaps,
    "verify_evidence": _tool_verify_evidence,
    "critique_claims": _tool_critique_claims,
    "build_paper_analysis_overview": _tool_build_paper_analysis_overview,
    "check_citation_completeness": _tool_check_citation_completeness,
    "evaluate_stop_conditions": _tool_evaluate_stop_conditions,
    "save_reading_card": _tool_save_reading_card,
    "export_report": _tool_export_report,
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
    # Full-text reading cards can send local PDF pages to the model only after
    # explicit consent. The deterministic local fallback stays available.
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
    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        raise ValueError(f"Agent 工具尚未实现：{tool_name}")
    return handler(payload)


__all__ = ["call_agent_tool"]

