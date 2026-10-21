"""Shared domain vocabulary independent of HTTP, SQLite, or UI code."""

PAPER_STATUSES = frozenset({"inbox", "to_read", "reading", "read", "later", "excluded", "watching"})
QUERY_DECISIONS = frozenset({"new", "relevant", "irrelevant", "ignored", "later"})
RECOMMENDATION_DECISIONS = frozenset({"relevant", "irrelevant", "later"})
# Supported discovery sources used by the federated search layer. Most are
# public scholarly connectors; Tavily is an optional, locally keyed web
# discovery source whose unverified hits remain review-only candidates.
# Keeping the vocabulary here makes the API, scheduler and UI validate them
# consistently.
SOURCE_NAMES = frozenset({"arxiv", "openalex", "crossref", "europepmc", "semanticscholar", "dblp", "openreview", "tavily", "deepseek_search"})
