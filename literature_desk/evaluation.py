"""Small, dependency-free ranking metrics for offline LitLoom evaluations.

The evaluator deliberately consumes stable candidate IDs and human labels. It
does not fetch papers, call a model, or infer relevance from metadata. That
makes a saved JSON set suitable for regression comparisons across connector,
ranker, and prompt versions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence


def _unique(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def precision_at_k(ranked: Sequence[object], relevant: Iterable[object], k: int) -> float | None:
    """Return precision for the first *k* rows, or ``None`` for empty input."""
    cutoff = max(1, int(k))
    top = _unique(ranked)[:cutoff]
    if not top:
        return None
    expected = set(_unique(relevant))
    return sum(item in expected for item in top) / len(top)


def recall_at_k(ranked: Sequence[object], relevant: Iterable[object], k: int) -> float | None:
    """Return recall for the first *k* rows, or ``None`` without labels."""
    expected = set(_unique(relevant))
    if not expected:
        return None
    top = set(_unique(ranked)[: max(1, int(k))])
    return len(top & expected) / len(expected)


def mean_reciprocal_rank(ranked: Sequence[object], relevant: Iterable[object]) -> float | None:
    """Return the reciprocal rank of the first relevant candidate."""
    expected = set(_unique(relevant))
    if not expected:
        return None
    for index, item in enumerate(_unique(ranked), start=1):
        if item in expected:
            return 1 / index
    return 0.0


def evaluate_ranked_cases(cases: Sequence[Mapping[str, object]], k_values: Sequence[int] = (5, 10)) -> dict:
    """Evaluate a fixed, human-labelled set of ranked candidate snapshots.

    Each case must contain ``ranked`` and ``relevant`` lists. ``id`` and
    ``query`` are carried into the report for auditability. Cases without
    labels are reported as excluded instead of being treated as negatives.
    """
    ks = tuple(sorted({max(1, int(k)) for k in k_values})) or (5,)
    per_case: list[dict] = []
    excluded: list[dict] = []
    for index, raw in enumerate(cases):
        if not isinstance(raw, Mapping):
            excluded.append({"index": index, "reason": "case_not_object"})
            continue
        ranked = raw.get("ranked")
        relevant = raw.get("relevant")
        if not isinstance(ranked, list) or not isinstance(relevant, list) or not _unique(relevant):
            excluded.append({"id": str(raw.get("id") or index), "reason": "missing_ranked_or_relevant_labels"})
            continue
        item = {"id": str(raw.get("id") or index), "query": str(raw.get("query") or ""), "label_count": len(_unique(relevant))}
        for k in ks:
            p = precision_at_k(ranked, relevant, k)
            r = recall_at_k(ranked, relevant, k)
            item[f"precision_at_{k}"] = round(p, 6) if p is not None else None
            item[f"recall_at_{k}"] = round(r, 6) if r is not None else None
        mrr = mean_reciprocal_rank(ranked, relevant)
        item["reciprocal_rank"] = round(mrr, 6) if mrr is not None else None
        per_case.append(item)

    def macro(field: str) -> float | None:
        values = [item[field] for item in per_case if item.get(field) is not None]
        return round(sum(values) / len(values), 6) if values else None

    metrics = {"cases": len(per_case), "excluded_cases": len(excluded), "mrr": macro("reciprocal_rank")}
    for k in ks:
        metrics[f"precision_at_{k}"] = macro(f"precision_at_{k}")
        metrics[f"recall_at_{k}"] = macro(f"recall_at_{k}")
    return {
        "status": "ready" if per_case else "insufficient_labels",
        "case_count": len(per_case),
        "excluded_count": len(excluded),
        "metrics": metrics,
        "per_case": per_case,
        "excluded": excluded,
    }


__all__ = ["evaluate_ranked_cases", "mean_reciprocal_rank", "precision_at_k", "recall_at_k"]
