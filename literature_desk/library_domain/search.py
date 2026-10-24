"""Compatibility facade for query, identity, ranking, and record helpers."""

from .query_terms import (
    norm_doi,
    norm_arxiv,
    title_key,
    relevance_tokens,
    expanded_relevance_terms,
    search_plan_terms,
    extract_search_facets,
    _bridge_query_terms,
    _bridge_non_latin_terms,
    search_query_variants,
)
from .identity_memory import (
    candidate_identity,
    candidate_identity_variants,
    record_discovery_search_memory,
    discovery_intent_key,
    _json_object,
    _discovery_feedback_by_key,
    _discovery_feedback_examples,
)
from .ranking import (
    title_similarity,
    merge_search_records,
    relevance_label,
    relevance_token_weight,
    score_relevance,
    _model_web_has_object_task_evidence,
    filter_model_web_quality,
    filter_agent_candidate_quality,
)
from .normalization import (
    clean_text,
    compact_search_raw,
    compact_candidate_record,
    normalize_record,
    paper_pdf_candidates,
    openalex_public_pdf_candidates,
    europepmc_public_pdf_candidates,
    semantic_scholar_public_pdf_candidates,
)

__all__ = ['norm_doi', 'norm_arxiv', 'title_key', 'relevance_tokens', 'expanded_relevance_terms', 'search_plan_terms', 'extract_search_facets', '_bridge_query_terms', '_bridge_non_latin_terms', 'search_query_variants', 'candidate_identity', 'candidate_identity_variants', 'record_discovery_search_memory', 'discovery_intent_key', '_json_object', '_discovery_feedback_by_key', '_discovery_feedback_examples', 'title_similarity', 'merge_search_records', 'relevance_label', 'relevance_token_weight', 'score_relevance', '_model_web_has_object_task_evidence', 'filter_model_web_quality', 'filter_agent_candidate_quality', 'clean_text', 'compact_search_raw', 'compact_candidate_record', 'normalize_record', 'paper_pdf_candidates', 'openalex_public_pdf_candidates', 'europepmc_public_pdf_candidates', 'semantic_scholar_public_pdf_candidates']
