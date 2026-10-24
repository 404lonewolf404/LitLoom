"""Compatibility facade for federated discovery search.

The implementation is grouped by planning, execution, and background-job
responsibilities while this module preserves the historical import path.
"""

from .search_planning import (
    library_relation_variants,
    library_term_variants,
    search_query_plan,
    normalize_search_options,
    prepare_user_search_options,
    search_persists_results,
    record_matches_search_options,
    rank_fusion_score,
    diversify_results,
    _prioritize_ai_search_variants,
    _prefetch_source_variants,
)
from .search_execution import (
    run_query,
)
from .search_jobs import (
    launch_query_job,
    get_query_cadence,
)

__all__ = ['library_relation_variants', 'library_term_variants', 'search_query_plan', 'normalize_search_options', 'prepare_user_search_options', 'search_persists_results', 'record_matches_search_options', 'rank_fusion_score', 'diversify_results', '_prioritize_ai_search_variants', '_prefetch_source_variants', 'run_query', 'launch_query_job', 'get_query_cadence']
