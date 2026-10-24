"""Compatibility facade for research evidence and document tools."""

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

__all__ = ['_tool_prepare_fulltext', '_tool_download_fulltext', '_tool_extract_pdf_pages', '_tool_ocr_pdf_pages', '_tool_render_pdf_page', '_tool_search_document', '_tool_expand_citations', '_tool_track_follow_up', '_tool_generate_reading_route', '_tool_compare_papers', '_tool_resolve_comparison_gaps', '_tool_generate_research_brief']
