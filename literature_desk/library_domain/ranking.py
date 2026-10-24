"""Candidate merging, relevance scoring, and quality gates."""

from __future__ import annotations

from ..services import app_services as _services

def title_similarity(left: str, right: str) -> float:
    """Compare normalized titles without treating different editions as equal."""

    def terms(value: str) -> set[str]:
        normalized = _services().title_key(value)
        found = set(normalized.split())
        for run in _services().re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
            found.update(
                run[index : index + 2] for index in range(max(0, len(run) - 1))
            )
        return found

    left_terms = terms(left)
    right_terms = terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / max(1, len(left_terms | right_terms))


def merge_search_records(records: list[dict]) -> tuple[list[dict], int]:
    """Merge exact and near-identical hits without returning provider aliases.

    Search providers often expose one work as a journal DOI, an arXiv
    preprint, and a web result.  Strong identifiers are still kept as fields
    on the merged candidate, but a near-identical cross-type DOI/arXiv hit is
    shown once.  Clearly different identifiers of the same type remain
    separate because they may be genuinely different editions or corrections.
    """
    merged = {}
    duplicate_count = 0
    for record in records:
        key = _services().candidate_identity(record)
        previous = merged.get(key)
        if previous is None:
            merged[key] = record
            continue
        duplicate_count += 1
        # Prefer the record carrying more evidence, while retaining the
        # original source/query path on the selected item.
        previous_quality = (
            bool(previous.get("doi") or previous.get("arxiv_id")),
            len(previous.get("abstract", "")),
            len(previous.get("authors", "")),
        )
        current_quality = (
            bool(record.get("doi") or record.get("arxiv_id")),
            len(record.get("abstract", "")),
            len(record.get("authors", "")),
        )
        if current_quality > previous_quality:
            merged[key] = {**previous, **record}
    # A second pass catches title punctuation and provider-specific subtitle
    # differences.  The candidate set is bounded by connector page limits.
    keys = list(merged)
    for index, left_key in enumerate(keys):
        left = merged.get(left_key)
        if not left or not left.get("title"):
            continue
        for right_key in keys[index + 1 :]:
            right = merged.get(right_key)
            if not right or not right.get("title"):
                continue
            left_year, right_year = left.get("year"), right.get("year")
            try:
                years_conflict = (
                    bool(left_year and right_year)
                    and abs(int(left_year) - int(right_year)) > 1
                )
            except (TypeError, ValueError):
                years_conflict = False
            left_strong = bool(left.get("doi") or left.get("arxiv_id"))
            right_strong = bool(right.get("doi") or right.get("arxiv_id"))
            similarity = _services().title_similarity(left["title"], right["title"])
            # A very close title is enough when one provider omitted the year;
            # this is common for Tavily HTML pages.  Lower-similarity matches
            # still require a consistent year to avoid accidental merges.
            if (
                years_conflict
                or (not left_year or not right_year)
                and similarity < 0.97
            ):
                continue
            if similarity < 0.92:
                continue
            left_doi, right_doi = _services().norm_doi(
                left.get("doi", "")
            ), _services().norm_doi(right.get("doi", ""))
            left_arxiv, right_arxiv = _services().norm_arxiv(
                left.get("arxiv_id", "")
            ), _services().norm_arxiv(right.get("arxiv_id", ""))
            different_same_type = bool(
                (left_doi and right_doi and left_doi != right_doi)
                or (left_arxiv and right_arxiv and left_arxiv != right_arxiv)
            )
            cross_type_versions = bool(
                (left_doi and right_arxiv) or (right_doi and left_arxiv)
            )
            # Different DOI-only or arXiv-only identifiers are retained.  A
            # DOI + arXiv crosswalk with an almost identical title is the
            # common preprint/journal representation of one work, so merge it.
            if different_same_type and not cross_type_versions:
                continue
            if (
                left_strong
                and right_strong
                and not cross_type_versions
                and similarity < 0.985
            ):
                continue
            previous_quality = (
                left_strong,
                len(left.get("abstract", "")),
                len(left.get("authors", "")),
            )
            current_quality = (
                right_strong,
                len(right.get("abstract", "")),
                len(right.get("authors", "")),
            )
            winner, loser = (
                (right, left) if current_quality > previous_quality else (left, right)
            )
            combined = dict(loser)
            # Do not let an empty field from the better-scored provider erase
            # useful metadata from the other provider.
            for field, value in winner.items():
                if value not in (None, "", [], {}):
                    combined[field] = value
            for field, value in loser.items():
                if combined.get(field) in (None, "", [], {}) and value not in (
                    None,
                    "",
                    [],
                    {},
                ):
                    combined[field] = value
            combined["doi"] = _services().norm_doi(combined.get("doi", ""))
            combined["arxiv_id"] = _services().norm_arxiv(combined.get("arxiv_id", ""))
            combined["_dedup_aliases"] = sorted(
                set(
                    (left.get("_dedup_aliases") or [])
                    + (right.get("_dedup_aliases") or [])
                    + [left_key, right_key]
                )
            )
            for list_field in ("provider_sources", "source_citations", "search_paths"):
                values = []
                for item in (left.get(list_field) or []) + (
                    right.get(list_field) or []
                ):
                    if item not in values:
                        values.append(item)
                if values:
                    combined[list_field] = values
            merged[left_key] = combined
            merged.pop(right_key, None)
            duplicate_count += 1
            left = merged[left_key]
    return list(merged.values()), duplicate_count


def relevance_label(score: int) -> str:
    if score < 10:
        return "待核验"
    if score >= 60:
        return "高相关"
    if score >= 35:
        return "邻近方向"
    return "低相关"


def relevance_token_weight(term: str) -> float:
    """Down-weight short Chinese n-grams while retaining their recall value."""
    if _services().re.fullmatch(r"[\u4e00-\u9fff]+", term or ""):
        if len(term) == 2:
            return 0.35
        if len(term) == 3:
            return 0.60
    return 1.0


def score_relevance(
    candidate: dict,
    query: str,
    known: list[sqlite3.Row],
    feedback: list[sqlite3.Row] | None = None,
) -> dict:
    """Return an explainable triage score against a query and library memory."""
    query_terms = _services().relevance_tokens(query)
    expanded_terms = _services().expanded_relevance_terms(query)
    bridge_terms = expanded_terms - query_terms
    title_terms = _services().relevance_tokens(candidate.get("title", ""))
    body_terms = _services().relevance_tokens(
        " ".join([candidate.get("abstract", ""), candidate.get("authors", "")])
    )
    candidate_terms = title_terms | body_terms
    title_hits = sorted(expanded_terms & title_terms)
    body_hits = sorted(expanded_terms & body_terms)
    original_title_hits = sorted(query_terms & title_terms)
    bridge_title_hits = sorted(set(title_hits) & bridge_terms)
    score = min(
        55,
        round(
            sum(
                _services().relevance_token_weight(term) for term in original_title_hits
            )
            * 22
            + sum(
                _services().relevance_token_weight(term) for term in bridge_title_hits
            )
            * 14
            + sum(
                _services().relevance_token_weight(term)
                for term in query_terms & body_terms
            )
            * 7
            + sum(
                _services().relevance_token_weight(term)
                for term in set(body_hits) & bridge_terms
            )
            * 5
        ),
    )
    reasons = []
    if original_title_hits:
        reasons.append("标题命中 " + ", ".join(original_title_hits[:5]))
    if bridge_title_hits:
        reasons.append("标题命中桥接概念 " + ", ".join(bridge_title_hits[:5]))
    if body_hits and not title_hits:
        reasons.append("摘要/作者命中 " + ", ".join(body_hits[:5]))
    phrases = [
        _services().clean_text(value, 120).casefold()
        for value in _services().re.findall(r'"([^"\n]+)"', query)
        if _services().clean_text(value, 120)
    ]
    title_lower = _services().clean_text(candidate.get("title", ""), 2000).casefold()
    abstract_lower = (
        _services().clean_text(candidate.get("abstract", ""), 30000).casefold()
    )
    phrase_hits = [
        phrase
        for phrase in phrases
        if phrase in title_lower or phrase in abstract_lower
    ]
    if phrase_hits:
        score += min(22, 11 * len(phrase_hits))
        reasons.append("精确短语命中 " + ", ".join(phrase_hits[:3]))
    if expanded_terms:
        coverage = len(set(title_hits + body_hits)) / max(1, len(expanded_terms))
        if coverage >= 0.4:
            score += 10
            reasons.append(f"查询概念覆盖 {round(coverage * 100)}%")
    # Long natural-language queries usually contain a research object, a task
    # and one or more methods.  Matching a method alone (for example an
    # attention-based car detector) is not enough for a food-volume query.
    # Score facet coverage explicitly and cap method-only candidates so source
    # rank cannot push them above papers that answer the actual task.
    facets = _services().extract_search_facets(query)
    facet_hits = {}
    facet_score_cap = 100
    object_alias_tokens = set()
    object_strong_tokens = set()
    task_strong_tokens = set()
    if facets.get("对象"):
        object_alias_tokens = _services().relevance_tokens(
            " ".join(facets["对象"].get("expansions", [])[:8])
        )
        object_strong_tokens = object_alias_tokens - {
            "image",
            "visual",
            "recognition",
            "computer",
            "vision",
        }
    if facets.get("任务"):
        task_strong_tokens = _services().relevance_tokens(
            " ".join(facets["任务"].get("expansions", [])[:8])
        ) & {"volume", "portion", "measurement", "quantification", "size"}
    for facet_name, facet in facets.items():
        aliases = set(
            _services().relevance_tokens(" ".join(facet.get("expansions", [])[:8]))
        )
        # Phrases such as “food volume estimation” contain the object word
        # food.  Do not let that object token falsely count as a task hit.
        if facet_name == "任务" and object_alias_tokens:
            aliases -= object_alias_tokens
        hits = aliases & candidate_terms
        if facet_name == "对象":
            hits &= object_strong_tokens
        if hits:
            facet_hits[facet_name] = sorted(hits)
            score += 6 if facet_name in {"对象", "任务"} else 3
            reasons.append(f"命中{facet_name}：" + ", ".join(sorted(hits)[:4]))
    has_object = bool(facets.get("对象"))
    has_task = bool(facets.get("任务"))
    object_hit = "对象" in facet_hits
    task_hit = "任务" in facet_hits
    title_facet_terms = _services().relevance_tokens(candidate.get("title", ""))
    title_object_hit = bool(object_strong_tokens & title_facet_terms)
    title_task_hit = bool(task_strong_tokens & title_facet_terms)
    body_object_hit = bool(object_strong_tokens & body_terms)
    body_task_hit = bool(task_strong_tokens & body_terms)
    if has_object and has_task:
        if object_hit and task_hit:
            score += 14
            reasons.append("对象与核心任务同时命中")
            if not title_object_hit and not title_task_hit:
                # Both concepts appearing only in a long abstract is weaker
                # evidence than a title-level match; this prevents incidental
                # mentions from outranking papers whose title answers the task.
                facet_score_cap = min(facet_score_cap, 34)
                reasons.append("对象与任务仅在摘要中出现，降低优先级")
            elif not title_object_hit or not title_task_hit:
                facet_score_cap = min(facet_score_cap, 58)
        elif not object_hit and not task_hit:
            facet_score_cap = 12
            reasons.append("未命中研究对象或核心任务，降为低相关")
        else:
            facet_score_cap = 27
            reasons.append("只命中对象或任务之一，需人工核验")
    elif has_task and not task_hit:
        facet_score_cap = 24
        reasons.append("未命中核心任务词，避免仅凭方法词推荐")
    if has_object and object_hit and not object_strong_tokens & candidate_terms:
        facet_score_cap = min(facet_score_cap, 24)
    if bridge_title_hits or set(body_hits) & bridge_terms:
        reasons.append("已用中英文概念桥接校正相关度")
    best_overlap = 0
    best_title = ""
    for row in known:
        other_terms = _services().relevance_tokens(
            " ".join([row["title"] or "", row["abstract"] or ""])
        )
        if not other_terms or not (title_terms or body_terms):
            continue
        overlap = len((title_terms | body_terms) & other_terms) / max(
            1, len(title_terms | body_terms)
        )
        if overlap > best_overlap:
            best_overlap, best_title = overlap, row["title"]
    if best_overlap >= 0.35:
        score += min(35, round(best_overlap * 55))
        reasons.append(
            "与库内论文有主题重合" + (f"：{best_title[:42]}" if best_title else "")
        )
    elif best_overlap >= 0.18:
        score += 12
        reasons.append("与已有主题存在部分重合")
    for row in feedback or []:
        other_terms = _services().relevance_tokens(
            " ".join([row["title"] or "", row["abstract"] or ""])
        )
        if not other_terms or not (title_terms or body_terms):
            continue
        overlap = len((title_terms | body_terms) & other_terms) / max(
            1, len(title_terms | body_terms)
        )
        if overlap >= 0.28 and row["user_decision"] == "relevant":
            score += 12
            reasons.append("根据你的主题反馈加权")
            break
        if overlap >= 0.28 and row["user_decision"] in {"irrelevant", "ignored"}:
            score -= 18
            reasons.append("根据你此前的忽略反馈降权")
            break
    if candidate.get("doi") or candidate.get("arxiv_id"):
        score += 5
    if candidate.get("open_access"):
        score += 2
    score = min(score, facet_score_cap)
    score = max(0, min(100, score))
    return {
        "score": score,
        "label": _services().relevance_label(score),
        "reasons": reasons or ["未发现明确词项重合，建议人工快速扫读"],
    }


def _model_web_has_object_task_evidence(paper: dict, facets: dict) -> bool:
    """Require a concrete object/task signal before exposing a model-web hit.

    Model-led search is intentionally broad, but lexical scoring can mistake
    phrases such as ``food security`` for a food-object paper.  Use the title
    and abstract as a cheap local guard before a candidate reaches the review
    queue; this does not replace the model verdict, it prevents obvious
    cross-domain false positives from being promoted as "adjacent" work.
    """
    text = (
        _services()
        .clean_text(f"{paper.get('title', '')} {paper.get('abstract', '')}", 12000)
        .casefold()
    )
    object_terms = [
        _services().clean_text(value, 120).casefold()
        for value in ((facets.get("对象") or {}).get("expansions") or [])
        if _services().clean_text(value, 120)
    ]
    task_terms = [
        _services().clean_text(value, 120).casefold()
        for value in ((facets.get("任务") or {}).get("expansions") or [])
        if _services().clean_text(value, 120)
    ]
    if not object_terms or not task_terms:
        return True
    if any(term in {"food", "meal", "dish"} for term in object_terms):
        # ``food security``, ``food crop`` and ``food yield`` are common
        # distractors for food-volume/portion questions. Require a phrase
        # that denotes an actual edible object, scene, or dietary measurement.
        object_hit = any(
            pattern in text
            for pattern in (
                "food volume",
                "food portion",
                "food image",
                "food item",
                "food intake",
                "food segmentation",
                "food recognition",
                "food mesh",
                "food reconstruction",
                "meal",
                "dish",
                "dietary",
                "nutrition",
                "plate",
                "edible",
                "eating",
            )
        )
    else:
        object_hit = any(term in text for term in object_terms)
    # Prefer the requested task terms, then fall back to a small set of common
    # measurement verbs so a phrase like "food volume" is still recognized.
    task_hit = any(term in text for term in task_terms if term not in object_terms)
    if not task_hit:
        task_hit = bool(
            _services().re.search(
                r"\b(?:volume|portion|measurement|measure|estimation|estimate|quantification|classification|recognition|segmentation|detection|prediction|reconstruction)\b",
                text,
            )
        )
    return object_hit and task_hit


def filter_model_web_quality(
    results: list[dict], query: str, options: dict
) -> tuple[list[dict], int]:
    """Keep model-web candidates that have evidence for the asked object/task.

    A model may deliberately broaden a later search to avoid repeats. Without a
    guard, shared method words (for example ``attention`` and ``estimation``)
    can turn an unrelated paper into a visible candidate. For a structured
    natural-language question, an empty result with an explanation is safer
    than filling the review queue with false positives. Exploratory mode keeps
    the old broad behavior by design.
    """
    if (
        not results
        or _services().clean_text(options.get("search_backend", ""), 40).casefold()
        != "model_web"
    ):
        return results, 0
    if (
        _services().clean_text(options.get("discovery_mode", "balanced"), 40).casefold()
        == "explore"
    ):
        return results, 0
    facets = _services().extract_search_facets(query)
    if not facets.get("对象") or not facets.get("任务"):
        return results, 0
    kept = []
    filtered = 0
    for paper in results:
        try:
            score = int(paper.get("relevance_score") or 0)
        except (TypeError, ValueError):
            score = 0
        model_label = (
            _services().clean_text(paper.get("model_relevance_label"), 40).casefold()
        )
        reasons = (
            paper.get("relevance_reasons")
            if isinstance(paper.get("relevance_reasons"), list)
            else []
        )
        if paper.get("relevance_reasons") is not reasons:
            paper["relevance_reasons"] = reasons
        has_facet_evidence = any(
            text.startswith(("命中对象：", "命中任务：", "对象与核心任务同时命中"))
            for text in (_services().clean_text(item, 300) for item in reasons)
        )
        has_object_task_evidence = _services()._model_web_has_object_task_evidence(
            paper, facets
        )
        selection_backfilled = bool(
            paper.get("selection_backfilled") or paper.get("_selection_backfilled")
        )
        # A candidate restored from the durable queue belongs to this exact
        # research intent.  It was already visible in an earlier run, which
        # means it passed that run's model-web quality gate.  Re-running the
        # current lexical guard here used to throw away the very history rows
        # that were added to prevent a repeated search from shrinking to a
        # handful of papers (for example when an older abstract used a
        # synonym that the current facet extractor does not know).  Trust the
        # durable acceptance marker, while keeping legacy rows conservative:
        # rows without the marker still need the normal evidence check.
        history_quality_accepted = bool(paper.get("history_quality_accepted"))
        # A direct model verdict can rescue a synonym-heavy title, but still
        # needs a minimal lexical signal. Adjacent results must clear the
        # normal threshold and expose at least one requested facet.
        accepted = (
            history_quality_accepted
            or (model_label == "direct" and score >= 25 and has_object_task_evidence)
            or (score >= 35 and has_facet_evidence and has_object_task_evidence)
            # A bounded fill is intentionally conservative, but it should not
            # disappear just because the model's final JSON ranked fewer rows
            # than requested.  It already passed the object/task evidence
            # check in _model_search_backfill_selection; retain it as an
            # explicit uncertain candidate for human review.
            or (selection_backfilled and score >= 12 and has_object_task_evidence)
        )
        if accepted:
            if history_quality_accepted:
                reasons = paper.setdefault("relevance_reasons", [])
                message = "同一研究主题的历史候选此前已通过相关性筛选，本轮作为历史补位保留；请人工确认是否需要再次阅读"
                if message not in reasons:
                    reasons.append(message)
            if selection_backfilled and model_label not in {"direct", "adjacent"}:
                paper["model_relevance_label"] = "uncertain"
                paper["relevance_label"] = "邻近方向" if score >= 35 else "跨领域线索"
                reasons = paper.setdefault("relevance_reasons", [])
                message = (
                    "AI 主筛选结果不足请求数量，已从真实工具候选补位；建议人工核验"
                )
                if message not in reasons:
                    reasons.append(message)
            # Persist the decision on every accepted model-web candidate so a
            # later repeat can reuse it without re-running a stricter or
            # differently configured lexical gate.
            paper["history_quality_accepted"] = True
            kept.append(paper)
        else:
            filtered += 1
    return kept, filtered


def filter_agent_candidate_quality(
    results: list[dict], query: str
) -> tuple[list[dict], dict]:
    """Apply the same object/task guard to Agent connector searches.

    Agent searches use the normal academic connectors so they can keep working
    when model-web search is unavailable.  That path used to bypass the
    model-web quality gate, which allowed method-only hits (for example a
    medical or finance paper containing ``attention`` and ``estimation``) into
    the main candidate set.  This local, deterministic pass is intentionally
    conservative: it removes only candidates with no evidence for either the
    requested object and task, while retaining adjacent papers that expose at
    least one requested facet for human review.

    The returned summary is persisted by the Agent step and gives the UI an
    explanation even when every candidate is filtered out.
    """
    items = [item for item in (results or []) if isinstance(item, dict)]
    facets = _services().extract_search_facets(query)
    summary = {
        "input_count": len(items),
        "kept_count": len(items),
        "filtered_count": 0,
        "direct": 0,
        "adjacent": 0,
        "uncertain": 0,
        "unrelated": 0,
        "facets": {key: value.get("hits", []) for key, value in facets.items()},
        "filtered_reasons": [],
    }
    # A short keyword query may not contain separable object/task facets.  In
    # that case the connector's normal ranking is the safer recall-preserving
    # behavior; the summary still tells the user why no extra gate ran.
    if not facets.get("对象") or not facets.get("任务"):
        summary["mode"] = "connector_ranking_only"
        return items, summary

    kept = []
    for item in items:
        score = item.get("relevance_score")
        try:
            score = (
                float(score)
                if score is not None
                else float(_services().score_relevance(item, query, [], [])["score"])
            )
        except (TypeError, ValueError):
            score = float(_services().score_relevance(item, query, [], [])["score"])
        evidence = _services()._model_web_has_object_task_evidence(item, facets)
        title_body = (
            _services()
            .clean_text(f"{item.get('title', '')} {item.get('abstract', '')}", 12000)
            .casefold()
        )
        object_terms = [
            _services().clean_text(value, 120).casefold()
            for value in [
                *((facets.get("对象") or {}).get("hits") or []),
                *((facets.get("对象") or {}).get("expansions") or []),
            ]
            if _services().clean_text(value, 120)
        ]
        task_terms = [
            _services().clean_text(value, 120).casefold()
            for value in [
                *((facets.get("任务") or {}).get("hits") or []),
                *((facets.get("任务") or {}).get("expansions") or []),
            ]
            if _services().clean_text(value, 120)
        ]
        object_hit = any(term in title_body for term in object_terms)
        task_hit = any(term in title_body for term in task_terms)
        if evidence and score >= 60:
            relation = "direct"
        elif evidence and (object_hit or task_hit) and score >= 24:
            relation = "adjacent"
        elif evidence and score >= 18:
            relation = "uncertain"
        else:
            relation = "unrelated"
        item["agent_relevance_class"] = relation
        item["agent_relevance_score"] = round(score, 2)
        if relation == "unrelated":
            summary["filtered_count"] += 1
            summary["unrelated"] += 1
            reason = "标题和摘要没有同时提供研究对象与核心任务证据"
            item["quality_filter_reason"] = reason
            if len(summary["filtered_reasons"]) < 8:
                summary["filtered_reasons"].append(
                    {
                        "title": _services().clean_text(item.get("title"), 240),
                        "reason": reason,
                    }
                )
            continue
        summary[relation] += 1
        existing_reasons = item.get("relevance_reasons")
        item["relevance_reasons"] = (
            list(existing_reasons)
            if isinstance(existing_reasons, list)
            else (
                [_services().clean_text(existing_reasons, 500)]
                if existing_reasons
                else []
            )
        )
        item["relevance_reasons"].append(
            {
                "direct": "Agent 质量闸门：研究对象与核心任务均有证据",
                "adjacent": "Agent 质量闸门：至少命中一个研究维度，保留作邻近线索",
                "uncertain": "Agent 质量闸门：有弱证据，保留并标记为待核验",
            }[relation]
        )
        kept.append(item)
    summary["kept_count"] = len(kept)
    summary["mode"] = "object_task_quality_gate"
    summary["gaps"] = []
    if not kept and items:
        summary["gaps"].append("本轮候选均未同时满足研究对象与核心任务证据")
        summary["gaps"].append(
            "可改写研究问题、放宽任务词，或切换到探索模式查看跨领域线索"
        )
    elif len(kept) < 5:
        summary["gaps"].append("相关候选数量较少，建议用同义词或限制条件补搜")
    return kept, summary


__all__ = ['title_similarity', 'merge_search_records', 'relevance_label', 'relevance_token_weight', 'score_relevance', '_model_web_has_object_task_evidence', 'filter_model_web_quality', 'filter_agent_candidate_quality']
