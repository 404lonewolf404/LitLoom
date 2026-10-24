"""Query normalization, multilingual term expansion, and search variants."""

from __future__ import annotations

from ..services import app_services as _services

def norm_doi(value: str) -> str:
    value = (value or "").strip().lower()
    value = _services().re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", value)
    return value.strip().rstrip(".,; ")


def norm_arxiv(value: str) -> str:
    value = (value or "").strip().lower()
    value = _services().re.sub(r"^(https?://)?(www\.)?arxiv\.org/(abs|pdf)/", "", value)
    value = _services().re.sub(r"\.pdf$", "", value)
    value = _services().re.sub(r"v\d+$", "", value)
    return value.strip()


def title_key(value: str) -> str:
    value = (value or "").lower()
    value = _services().re.sub(r"[^\w\s]", " ", value, flags=_services().re.UNICODE)
    return " ".join(value.split())


def relevance_tokens(value: str) -> set[str]:
    raw = (value or "").lower()
    words = _services().re.findall(r"[a-z0-9][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", raw)
    terms = {
        w.strip("_-") for w in words if w.strip("_-") not in _services().REL_STOPWORDS
    }
    # Chinese titles rarely use spaces.  Keep the complete phrase for
    # explainability, then add bounded bi/tri-grams so a query such as
    # “多模态推理” can match “多模态模型推理” without an embedding service.
    for run in _services().re.findall(r"[\u4e00-\u9fff]{2,}", raw):
        if len(run) > 2 and run not in _services().REL_STOPWORDS:
            for width in (2, 3):
                if len(run) < width:
                    continue
                terms.update(
                    run[index : index + width] for index in range(len(run) - width + 1)
                )
    return {term for term in terms if term not in _services().REL_STOPWORDS}


def expanded_relevance_terms(value: str) -> set[str]:
    """Return original and curated bridge terms for multilingual ranking."""
    raw = _services().clean_text(value, 2000)
    terms = set(_services().relevance_tokens(raw))
    lowered = raw.casefold()
    for concept, aliases in _services().SEARCH_CONCEPT_ALIASES.items():
        if concept.casefold() in lowered:
            terms.update(_services().relevance_tokens(" ".join(aliases[:3])))
    for facet in _services().extract_search_facets(raw).values():
        terms.update(
            _services().relevance_tokens(" ".join(facet.get("expansions", [])[:4]))
        )
    return terms


def search_plan_terms(value: str) -> tuple[list[str], list[str]]:
    """Expose meaningful concepts to search planners without CJK n-gram noise."""
    raw = _services().clean_text(value, 1000)
    if not raw:
        return [], []
    lowered = raw.casefold()
    concepts = []
    expanded = []

    def add(target: list[str], candidate: str) -> None:
        candidate = _services().clean_text(candidate, 200)
        if candidate and candidate.casefold() not in {
            item.casefold() for item in target
        }:
            target.append(candidate)

    matched_concepts = []
    for concept in sorted(_services().SEARCH_CONCEPT_ALIASES, key=len, reverse=True):
        normalized = concept.casefold()
        if _services().re.search(r"[\u4e00-\u9fff]", concept):
            matched = normalized in lowered
        else:
            matched = bool(
                _services().re.search(
                    r"(?<![a-z0-9])"
                    + _services().re.escape(normalized)
                    + r"(?![a-z0-9])",
                    lowered,
                )
            )
        if matched:
            matched_concepts.append(concept)
            add(concepts, concept)

    facets = _services().extract_search_facets(raw)
    for facet in facets.values():
        for hit in facet.get("hits", []):
            add(concepts, hit)
    for phrase in _services().re.findall(r'"([^"\n]+)"', raw):
        add(concepts, phrase)
    english_terms = [
        term
        for term in _services().re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", lowered)
        if len(term) >= 3 and term not in _services().REL_STOPWORDS
    ]
    for term in english_terms:
        add(concepts, term)

    for concept in concepts:
        add(expanded, concept)
    for concept in matched_concepts:
        for alias in _services().SEARCH_CONCEPT_ALIASES.get(concept, ()):
            add(expanded, alias)
    for facet in facets.values():
        for alias in facet.get("expansions", []):
            add(expanded, alias)
    for term in english_terms:
        add(expanded, term)
    return concepts[:24], expanded[:36]


def extract_search_facets(query: str) -> dict:
    """Extract conservative research facets from a mixed-language question."""
    raw = _services().clean_text(query, 1000)
    lowered = raw.casefold()
    facets = {}
    for facet, terms in _services().SEARCH_FACET_ALIASES.items():
        hits = []
        expansions = []
        for term, aliases in terms.items():
            if term.casefold() in lowered:
                hits.append(term)
                expansions.extend(aliases)
            elif _services().re.search(
                r"\b" + _services().re.escape(term.casefold()) + r"\b", lowered
            ):
                hits.append(term)
                expansions.extend(aliases)
        if hits:
            facets[facet] = {
                "hits": list(dict.fromkeys(hits)),
                "expansions": list(dict.fromkeys(expansions))[:8],
            }
    return facets


def _bridge_query_terms(raw: str) -> str:
    """Replace known Chinese/English concepts in one pass, longest first."""
    candidate = raw
    candidate = _services().re.sub(
        r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])", " ", candidate
    )
    candidate = _services().re.sub(
        r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", candidate
    )
    replacements = []
    for concept, aliases in _services().SEARCH_CONCEPT_ALIASES.items():
        if concept.casefold() in candidate.casefold() and aliases:
            replacements.append((concept, aliases[0]))
    for concept, alias in sorted(
        replacements, key=lambda pair: len(pair[0]), reverse=True
    ):
        candidate = _services().re.sub(
            _services().re.escape(concept),
            alias,
            candidate,
            count=1,
            flags=_services().re.IGNORECASE,
        )
    candidate = _services().re.sub(
        r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])", " ", candidate
    )
    candidate = _services().re.sub(
        r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", candidate
    )
    return _services().clean_text(candidate, 1000)


def _bridge_non_latin_terms(raw: str) -> str:
    """Bridge only Chinese terms, preserving intentional English aliases."""
    candidate = raw
    candidate = _services().re.sub(
        r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])", " ", candidate
    )
    candidate = _services().re.sub(
        r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", candidate
    )
    replacements = []
    for concept, aliases in _services().SEARCH_CONCEPT_ALIASES.items():
        if (
            _services().re.search(r"[\u4e00-\u9fff]", concept)
            and concept.casefold() in candidate.casefold()
            and aliases
        ):
            replacements.append((concept, aliases[0]))
    for concept, alias in sorted(
        replacements, key=lambda pair: len(pair[0]), reverse=True
    ):
        candidate = _services().re.sub(
            _services().re.escape(concept),
            alias,
            candidate,
            count=1,
            flags=_services().re.IGNORECASE,
        )
    candidate = _services().re.sub(
        r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])", " ", candidate
    )
    candidate = _services().re.sub(
        r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", candidate
    )
    return _services().clean_text(candidate, 1000)


def search_query_variants(
    query: str, max_variants: int = 6, depth: str = "balanced"
) -> list[dict]:
    """Build a deterministic, recall-oriented search plan.

    The primary wording is always retained.  Additional variants are simple
    provider-compatible phrases, so the union can be deduplicated across
    arXiv, OpenAlex, Crossref and Europe PMC without provider-specific syntax.
    ``focused`` keeps calls cheap, ``deep`` adds high-signal survey/benchmark
    lanes, and ``balanced`` is the default for long-running topics.
    """
    raw = _services().clean_text(query, 1000)
    if not raw:
        return []
    depth = depth if depth in _services().SEARCH_DEPTHS else "balanced"
    cap = max(1, min(int(max_variants), 12))
    if depth == "deep":
        cap = max(cap, 12)
    # Reserve room for the deep-discovery lanes; otherwise a broad alias list
    # could crowd out evaluation, limitations and frontier entry points.
    alias_limit = max(2, cap - 6) if depth == "deep" else cap
    variants = [
        {
            "id": "primary",
            "query": raw,
            "kind": "原始问题",
            "reason": "保留你的原始检索表达",
            "lane": "primary",
        }
    ]
    seen = {raw.casefold()}

    def add(candidate: str, kind: str, reason: str, lane: str):
        candidate = _services().clean_text(candidate, 1000)
        key = candidate.casefold()
        if not candidate or key in seen or len(variants) >= cap:
            return False
        seen.add(key)
        variants.append(
            {
                "id": f"variant-{len(variants)}",
                "query": candidate,
                "kind": kind,
                "reason": reason,
                "lane": lane,
            }
        )
        return True

    lowered = raw.casefold()
    # Build coherent field queries before one-term substitutions.  A literal
    # question such as “用深度学习模型卷积注意力等机制进行食物体积的估计”
    # must not be sent to scholarly APIs as one long Chinese sentence: most
    # indexes then match only “attention” or “convolution” and return unrelated
    # detection papers.  The facet frame keeps the object + task together and
    # treats methods as constraints rather than the main topic.
    facets = _services().extract_search_facets(raw)
    object_terms = list((facets.get("对象") or {}).get("expansions") or [])
    task_terms = list((facets.get("任务") or {}).get("expansions") or [])
    method_terms = list((facets.get("方法") or {}).get("expansions") or [])
    if task_terms:
        task = task_terms[0]
        method = method_terms[0] if method_terms else ""
        obj = object_terms[0] if object_terms else ""
        # The task bridge may already contain the object (food volume, portion
        # size), so avoid a noisy duplicate such as “food food volume …”.
        if obj and obj.casefold() not in task.casefold():
            task_with_object = f"{obj} {task}"
        else:
            task_with_object = task
        add(
            " ".join(part for part in (task_with_object, method) if part),
            "语义任务骨架",
            "同时保留研究对象、核心任务和方法约束",
            "semantic",
        )
        if obj and method_terms:
            second_task = task if obj.casefold() in task.casefold() else f"{obj} {task}"
            add(
                " ".join(dict.fromkeys([second_task, method_terms[-1]])),
                "对象任务方法",
                "用对象 + 任务 + 另一种方法表达召回同主题论文",
                "semantic",
            )
        # Image-based food estimation is often indexed with dietary-assessment
        # or portion-size wording rather than the exact Chinese translation.
        if obj and any(
            "volume" in term.casefold() or "portion" in term.casefold()
            for term in task_terms
        ):
            add(
                "food portion size estimation computer vision",
                "领域同义表达",
                "覆盖食物份量、图像测量和饮食评估文献的常见写法",
                "semantic",
            )

    bridge = _services()._bridge_query_terms(raw)
    if bridge.casefold() != raw.casefold():
        # A mixed Chinese/English bridge can be useful for debugging, but a
        # long literal sentence is a poor provider query.  Prefer the compact
        # semantic lanes above when they exist.
        if not task_terms or len(bridge.split()) <= 8:
            add(bridge, "英文桥接", "把已识别的中文术语桥接到英文索引表达", "bridge")

    # One-term substitutions are useful for compact keyword searches. For a
    # sentence with a recognized research task they create malformed mixed
    # Chinese/English queries and drown out the coherent object/task lanes.
    if not task_terms:
        for concept, aliases in _services().SEARCH_CONCEPT_ALIASES.items():
            if concept.casefold() not in lowered:
                continue
            for alias in aliases[:2]:
                candidate = _services().re.sub(
                    _services().re.escape(concept),
                    alias,
                    raw,
                    count=1,
                    flags=_services().re.IGNORECASE,
                )
                candidate = _services()._bridge_non_latin_terms(candidate)
                add(
                    candidate,
                    "概念别名",
                    f"用“{alias}”补召回“{concept}”的论文",
                    "alias",
                )
            if len(variants) >= alias_limit:
                break

    if depth != "focused":
        facet_terms = []
        for item in facets.values():
            facet_terms.extend(item["expansions"][:2])
        if facet_terms:
            add(
                " ".join(dict.fromkeys(facet_terms[:4])),
                "概念骨架",
                "按对象、方法、任务和评价词重组检索骨架",
                "facets",
            )
    if depth == "deep":
        base = bridge if bridge.casefold() != raw.casefold() else raw
        add(
            f"{base} survey",
            "综述入口",
            "补充综述与系统性地图，优先建立领域全貌",
            "survey",
        )
        add(f"{base} benchmark", "基准入口", "补充基准、数据集与评测论文", "benchmark")
        add(f"{base} empirical study", "实证入口", "补充实证比较和失败分析", "evidence")
        add(
            f"{base} dataset evaluation",
            "数据集评测入口",
            "补充数据集、评测协议和可比较实验",
            "evaluation",
        )
        add(
            f"{base} limitations failure analysis",
            "局限与失败入口",
            "主动寻找边界条件、失败案例和负结果",
            "limitations",
        )
        add(
            f"{base} state of the art",
            "前沿综述入口",
            "补充领域近期最佳结果与比较表",
            "frontier",
        )
    return variants[:cap]


__all__ = ['norm_doi', 'norm_arxiv', 'title_key', 'relevance_tokens', 'expanded_relevance_terms', 'search_plan_terms', 'extract_search_facets', '_bridge_query_terms', '_bridge_non_latin_terms', 'search_query_variants']
