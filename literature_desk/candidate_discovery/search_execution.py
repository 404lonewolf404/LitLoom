"""Federated source execution and candidate persistence."""

from __future__ import annotations

from ..services import app_services as _services
from .search_fetch import _fetch_search_sources
from .search_quality import _apply_search_quality_and_history
from .search_ranking import _rank_search_candidates
from .search_persistence import _persist_search_results

def run_query(
    query: str,
    sources: list[str],
    limit: int = 20,
    query_id: str = "",
    scheduled: bool = False,
    name: str = "",
    options: dict | None = None,
    request_id: str = "",
    plan_override: dict | None = None,
) -> dict:
    fetch_state = _fetch_search_sources(
        query, sources, limit, query_id, scheduled, name, options, request_id, plan_override
    )
    # A model-led search can return useful candidates even when one or more
    # of the optional connectors selected by the model timed out or was rate
    # limited.  Those source-level failures must participate in the durable
    # run state: otherwise the history page reports "completed" and hides the
    # retry action while the source detail quietly says "partial".  Keep the
    # candidates, but publish a partial run so the UI and scheduler can offer
    # an explicit retry.
    _apply_search_quality_and_history(fetch_state)
    _rank_search_candidates(fetch_state)
    _persist_search_results(fetch_state)
    return fetch_state.response


