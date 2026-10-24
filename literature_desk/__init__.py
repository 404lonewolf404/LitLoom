"""Domain contracts for LitLoom.

The current deployment remains a small modular monolith. Keeping stable
workflow constants in a package gives future API/worker splits one canonical
place for domain validation.
"""

from .domain import PAPER_STATUSES, QUERY_DECISIONS, RECOMMENDATION_DECISIONS, SOURCE_NAMES

__version__ = "0.20.0"

__all__ = ["PAPER_STATUSES", "QUERY_DECISIONS", "RECOMMENDATION_DECISIONS", "SOURCE_NAMES", "__version__"]
