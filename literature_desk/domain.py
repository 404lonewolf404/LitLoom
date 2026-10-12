"""Shared domain vocabulary independent of HTTP, SQLite, or UI code."""

PAPER_STATUSES = frozenset({"inbox", "to_read", "reading", "read", "later", "excluded", "watching"})
QUERY_DECISIONS = frozenset({"new", "relevant", "irrelevant", "ignored", "later"})
RECOMMENDATION_DECISIONS = frozenset({"relevant", "irrelevant", "later"})
# Public, keyless connectors used by the federated discovery layer.  Keeping
# the vocabulary in the domain module means the API, scheduler and UI reject
# unknown connectors consistently.
SOURCE_NAMES = frozenset({"arxiv", "openalex", "crossref", "europepmc", "semanticscholar", "dblp", "openreview"})
