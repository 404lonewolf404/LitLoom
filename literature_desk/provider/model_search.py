"""Compatibility facade for model assisted web search.

Model search is split into normalization, connector tools, orchestration, and
candidate refill modules while preserving this historic import path.
"""

from .model_search_normalization import (
    ModelWebSearchUnsupported,
    _model_search_url,
    _model_search_year,
    normalize_model_search_results,
)
from .model_search_tools import (
    _model_search_source_cooldown,
    _model_search_source_error,
    _model_search_tool_schema,
    _model_search_payload,
    _model_search_tool_calls,
    _model_search_append_assistant_turn,
    _model_search_selection_rows,
    _model_search_tool_arguments,
    _fast_model_search_source_call,
    _model_search_prefetch,
    _model_search_tool_result,
)
from .model_search_candidate_fill import (
    _model_search_backfill_selection,
    _model_search_local_fill,
)
from .model_search_orchestration import (
    _model_web_search_unbounded,
    model_web_search,
)
from .model_search_history import (
    _model_search_history_fill,
)

__all__ = ['ModelWebSearchUnsupported', '_model_search_url', '_model_search_year', 'normalize_model_search_results', '_model_search_source_cooldown', '_model_search_source_error', '_model_search_tool_schema', '_model_search_payload', '_model_search_tool_calls', '_model_search_append_assistant_turn', '_model_search_selection_rows', '_model_search_tool_arguments', '_fast_model_search_source_call', '_model_search_prefetch', '_model_search_tool_result', '_model_search_backfill_selection', '_model_search_local_fill', '_model_web_search_unbounded', 'model_web_search', '_model_search_history_fill']
