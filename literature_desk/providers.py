"""Compatibility facade for academic and model-assisted retrieval providers.

The implementation is grouped under :mod:`literature_desk.provider`; this module
keeps the historic ``literature_desk.providers`` import path stable for the app,
plugins, and external integrations.
"""

from .provider.public_sources import (
    DeepSeekResponseTooLargeError,
    _deepseek_tool_post,
    _openreview_value,
    _safe_public_web_url,
    _tavily_api_key,
    _tavily_extract_identifiers,
    _tavily_title_similarity,
    _verify_tavily_result,
    browse_public_web,
    deepseek_search_available,
    fetch_deepseek_page,
    model_search_available_connectors,
    search_arxiv,
    search_crossref,
    search_deepseek,
    search_dblp,
    search_europepmc,
    search_openalex,
    search_openreview,
    search_semanticscholar,
    search_tavily,
)
from .provider.model_search import (
    ModelWebSearchUnsupported,
    _fast_model_search_source_call,
    _model_search_append_assistant_turn,
    _model_search_backfill_selection,
    _model_search_history_fill,
    _model_search_local_fill,
    _model_search_payload,
    _model_search_prefetch,
    _model_search_selection_rows,
    _model_search_source_cooldown,
    _model_search_source_error,
    _model_search_tool_arguments,
    _model_search_tool_calls,
    _model_search_tool_result,
    _model_search_tool_schema,
    _model_search_url,
    _model_search_year,
    _model_web_search_unbounded,
    model_web_search,
    normalize_model_search_results,
)

__all__ = [
    "search_crossref", "search_arxiv", "search_openalex", "search_europepmc",
    "search_semanticscholar", "search_dblp", "_openreview_value", "search_openreview",
    "_tavily_api_key", "_tavily_title_similarity", "_tavily_extract_identifiers",
    "_verify_tavily_result", "search_tavily", "search_deepseek",
    "deepseek_search_available", "model_search_available_connectors",
    "DeepSeekResponseTooLargeError", "fetch_deepseek_page", "_deepseek_tool_post",
    "_safe_public_web_url", "browse_public_web", "ModelWebSearchUnsupported",
    "_model_search_url", "_model_search_year", "normalize_model_search_results",
    "_model_search_source_cooldown", "_model_search_source_error",
    "_model_search_tool_schema", "_model_search_payload", "_model_search_tool_calls",
    "_model_search_append_assistant_turn", "_model_search_selection_rows",
    "_model_search_tool_arguments", "_fast_model_search_source_call",
    "_model_search_prefetch", "_model_search_tool_result",
    "_model_search_backfill_selection", "_model_search_local_fill",
    "_model_web_search_unbounded", "model_web_search", "_model_search_history_fill",
]

